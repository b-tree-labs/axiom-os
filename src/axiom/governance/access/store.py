# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The sharing record: reads and writes of the three ``authz`` tables (ADR-263 D10).

Sessions come from ``session_for("authz")`` (ADR-052) unless a test binds its own
provider. Nothing here decides anything: :mod:`.decision` reads these rows to
answer, and is the only writer through :mod:`.sharing`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from axiom.governance.access.roles import RESTRICTED, SITE, Role

#: Access may expire on any date within a year (SHR-R1.2, SHR-R7.1).
MAX_EXPIRY = timedelta(days=366)

_provider: Callable[[], AbstractContextManager[Any]] | None = None


def set_session_provider(provider: Callable[[], AbstractContextManager[Any]] | None) -> None:
    """Bind the session the record is read and written through; ``None`` restores ``session_for``."""
    global _provider
    _provider = provider


@contextmanager
def session_scope() -> Iterator[Any]:
    if _provider is not None:
        with _provider() as session:
            yield session
        return
    from axiom.infra.db import session_for

    with session_for("authz") as session:
        yield session


def now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Grant:
    id: str
    site: str
    kind: str
    object_id: str
    subject: str
    role: Role
    expires_at: datetime | None
    granted_by: str
    granted_via: str
    granted_at: datetime

    def active(self, at: datetime | None = None) -> bool:
        when = at or now()
        return self.expires_at is None or self.expires_at > when

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "role": self.role.value,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "granted_by": self.granted_by,
            "granted_via": self.granted_via,
            "granted_at": self.granted_at.isoformat(),
        }


@dataclass(frozen=True)
class Settings:
    """General access and the two Google settings, with whether a row says so."""

    general: str = SITE
    general_role: Role = Role.VIEWER
    editors_can_share: bool = True
    viewers_can_download: bool = True
    #: ``False`` when no row exists and these are the home's projection.
    recorded: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "general": self.general,
            "general_role": self.general_role.value if self.general == SITE else None,
            "editors_can_share": self.editors_can_share,
            "viewers_can_download": self.viewers_can_download,
        }


def _grant(row: Any) -> Grant:
    return Grant(
        id=row.id, site=row.site, kind=row.object_kind, object_id=row.object_id, subject=row.subject,
        role=Role.parse(row.role), expires_at=_aware(row.expires_at), granted_by=row.granted_by,
        granted_via=row.granted_via, granted_at=_aware(row.granted_at) or now(),
    )


def _live(query: Any, at: datetime) -> Any:
    from axiom.extensions.builtins.authz.db_models import AccessGrant

    return query.filter(
        AccessGrant.revoked_at.is_(None),
        (AccessGrant.expires_at.is_(None)) | (AccessGrant.expires_at > at),
    )


def grants_on(session: Any, objects: Iterable[tuple[str, str]], *, subjects: Iterable[str] | None = None,
              at: datetime | None = None) -> list[Grant]:
    """The live grants on any of ``objects`` (``(kind, id)`` pairs), optionally to ``subjects`` only."""
    from sqlalchemy import and_, or_

    from axiom.extensions.builtins.authz.db_models import AccessGrant

    pairs = list(dict.fromkeys(objects))
    if not pairs:
        return []
    query = session.query(AccessGrant).filter(
        or_(*[and_(AccessGrant.object_kind == k, AccessGrant.object_id == i) for k, i in pairs])
    )
    if subjects is not None:
        names = list(dict.fromkeys(subjects))
        if not names:
            return []
        query = query.filter(AccessGrant.subject.in_(names))
    rows = _live(query, at or now()).order_by(AccessGrant.granted_at).all()
    return [_grant(r) for r in rows]


def objects_shared_with(session: Any, site: str, subjects: Iterable[str], *, kind: str | None = None,
                        at: datetime | None = None) -> list[Grant]:
    """Every live grant at ``site`` to any of ``subjects``: what is shared with a person or their groups."""
    from axiom.extensions.builtins.authz.db_models import AccessGrant

    names = list(dict.fromkeys(subjects))
    if not names:
        return []
    query = session.query(AccessGrant).filter(AccessGrant.site == site, AccessGrant.subject.in_(names))
    if kind:
        query = query.filter(AccessGrant.object_kind == kind)
    return [_grant(r) for r in _live(query, at or now()).order_by(AccessGrant.granted_at.desc()).all()]


def settings_of(session: Any, kind: str, object_id: str) -> Settings | None:
    from axiom.extensions.builtins.authz.db_models import AccessSettings

    row = session.get(AccessSettings, (kind, object_id))
    if row is None:
        return None
    return Settings(
        general=row.general, general_role=Role.parse(row.general_role), editors_can_share=bool(row.editors_can_share),
        viewers_can_download=bool(row.viewers_can_download), recorded=True,
    )


def settings_many(session: Any, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], Settings]:
    """The recorded settings for each of ``pairs`` that has a row."""
    from sqlalchemy import and_, or_

    from axiom.extensions.builtins.authz.db_models import AccessSettings

    want = list(dict.fromkeys(pairs))
    if not want:
        return {}
    rows = session.query(AccessSettings).filter(
        or_(*[and_(AccessSettings.object_kind == k, AccessSettings.object_id == i) for k, i in want])
    ).all()
    return {
        (r.object_kind, r.object_id): Settings(
            general=r.general, general_role=Role.parse(r.general_role), editors_can_share=bool(r.editors_can_share),
            viewers_can_download=bool(r.viewers_can_download), recorded=True,
        )
        for r in rows
    }


def write_settings(session: Any, *, kind: str, object_id: str, site: str, settings: Settings, by: str) -> None:
    from axiom.extensions.builtins.authz.db_models import AccessSettings

    if settings.general not in (RESTRICTED, SITE):
        raise ValueError(f"general access is {RESTRICTED!r} or {SITE!r}, not {settings.general!r}")
    row = session.get(AccessSettings, (kind, object_id))
    if row is None:
        row = AccessSettings(object_kind=kind, object_id=object_id, site=site)
        session.add(row)
    row.general = settings.general
    row.general_role = settings.general_role.value
    row.editors_can_share = settings.editors_can_share
    row.viewers_can_download = settings.viewers_can_download
    row.updated_by = by
    row.updated_at = now()


def add_grant(session: Any, *, site: str, kind: str, object_id: str, subject: str, role: Role,
              expires_at: datetime | None, by: str, via: str, receipt_id: str | None) -> tuple[Grant, Grant | None]:
    """Give ``subject`` ``role``; an earlier live grant to the same subject is replaced. Returns (new, replaced)."""
    from axiom.extensions.builtins.authz.db_models import AccessGrant

    at = now()
    before = _live(
        session.query(AccessGrant).filter(
            AccessGrant.object_kind == kind, AccessGrant.object_id == object_id, AccessGrant.subject == subject
        ),
        at,
    ).all()
    replaced = _grant(before[0]) if before else None
    for row in before:
        row.revoked_at, row.revoked_by, row.revoked_reason = at, by, "replaced"
    row = AccessGrant(
        id=str(uuid.uuid4()), site=site, object_kind=kind, object_id=object_id, subject=subject, role=role.value,
        expires_at=expires_at, granted_by=by, granted_via=via, granted_at=at, receipt_id=receipt_id,
    )
    session.add(row)
    return _grant(row), replaced


def revoke_grants(session: Any, *, kind: str, object_id: str, subject: str, by: str, reason: str = "removed") -> list[Grant]:
    from axiom.extensions.builtins.authz.db_models import AccessGrant

    at = now()
    rows = _live(
        session.query(AccessGrant).filter(
            AccessGrant.object_kind == kind, AccessGrant.object_id == object_id, AccessGrant.subject == subject
        ),
        at,
    ).all()
    for row in rows:
        row.revoked_at, row.revoked_by, row.revoked_reason = at, by, reason
    return [_grant(r) for r in rows]


def get_grant(session: Any, grant_id: str) -> tuple[Grant, bool] | None:
    """A grant by id, and whether it is still live."""
    from axiom.extensions.builtins.authz.db_models import AccessGrant

    row = session.get(AccessGrant, grant_id)
    if row is None:
        return None
    g = _grant(row)
    return g, row.revoked_at is None and g.active()


def expire_due(session: Any, *, at: datetime | None = None) -> list[Grant]:
    """End every grant whose day has come (SHR-R7.1): ``revoked_reason='expired'``. Returns those ended."""
    from axiom.extensions.builtins.authz.db_models import AccessGrant

    when = at or now()
    rows = (
        session.query(AccessGrant)
        .filter(AccessGrant.revoked_at.is_(None), AccessGrant.expires_at.isnot(None), AccessGrant.expires_at <= when)
        .all()
    )
    for row in rows:
        row.revoked_at, row.revoked_by, row.revoked_reason = when, "@keep", "expired"
    return [_grant(r) for r in rows]


def clean_expiry(raw: object, *, at: datetime | None = None) -> datetime | None:
    """An expiry from a date or ISO timestamp: in the future and within a year, or a refusal."""
    if raw in (None, ""):
        return None
    when = at or now()
    if isinstance(raw, datetime):
        value = _aware(raw)
    else:
        text = str(raw).strip()
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{raw!r} is not a date (use YYYY-MM-DD)") from None
        if len(text) == 10:  # a bare date ends at the end of that day
            value = value.replace(hour=23, minute=59, second=59)
        value = _aware(value)
    assert value is not None
    if value <= when:
        raise ValueError("an expiry date must be in the future")
    if value - when > MAX_EXPIRY:
        raise ValueError("access may expire at most a year from today")
    return value


__all__ = [
    "Grant",
    "MAX_EXPIRY",
    "Settings",
    "add_grant",
    "clean_expiry",
    "expire_due",
    "get_grant",
    "grants_on",
    "now",
    "objects_shared_with",
    "revoke_grants",
    "session_scope",
    "set_session_provider",
    "settings_many",
    "settings_of",
    "write_settings",
]
