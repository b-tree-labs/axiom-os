# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The one credential index every listing and resolve reads (ADR-218).

``axi secrets list`` and ``axi vault resolve --host`` used to read the same
metadata file and derive what they showed from it separately: the list
printed raw rows, while resolve worked out a host from ``git_host`` or
``issuer_url`` by itself and computed expiry health with its own date
parsing. A key that had been stored but carried no host showed up in the
list and was missing from resolve, and neither of them said why.

There is one reader now. A row's ``host``, ``purposes`` and ``health`` are
derived here, once, and every verb shows the same derivation:

    resolve --host H   is defined as   list --host H

so they cannot disagree. ``audit`` reads the same health.

The on-disk index keeps its path (``<state>/secrets/foreign-credentials.json``),
so an older release on the same machine reads the same file. Version 2
records ``host`` explicitly. :func:`migrate` upgrades version 1 in place,
adds fields and never removes one, keeps a copy of the version 1 file, and
does nothing when the file is already at version 2.

Metadata only. Nothing here opens a value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

#: The version a writer stamps. 1 had no ``host``; 2 records it.
INDEX_VERSION = 2

#: Days ahead within which a credential counts as ``expiring``. One number for
#: list, resolve and audit; audit's ``--within-days`` overrides it per call.
DEFAULT_WITHIN_DAYS = 14

#: Purpose hints, matched against a credential's notes. Kept as a small,
#: explicit table rather than a free-text search, because a fuzzy match that
#: picks the wrong token is the failure resolve exists to prevent.
PURPOSE_PATTERNS: dict[str, tuple[str, ...]] = {
    "git": ("read_repository", "write_repository", "git-only", "clone", "mirror"),
    "api": ("api scope", " api ", "api,", "automation", "self_rotate"),
    "registry": ("packaging", "upload", "pypi", "artifacts"),
    "runner": ("create_runner", "runner"),
}


# --------------------------------------------------------------------------- derivation


def derive_host(meta: dict[str, Any]) -> str:
    """The host a credential belongs to, lower-cased; ``""`` when nothing says.

    Order: a recorded ``host``, then ``git_host`` (the field the git helper
    keys on), then the hostname of ``issuer_url``. The store re-derives the
    recorded ``host`` whenever a call changes ``git_host`` or ``issuer_url``
    without naming a host, so a recorded host never goes stale behind them.
    """
    for key in ("host", "git_host"):
        raw = str(meta.get(key) or "").strip()
        if raw:
            return ((urlsplit(raw).hostname or raw) if "://" in raw else raw).lower()
    issuer = str(meta.get("issuer_url") or "").strip()
    return (urlsplit(issuer).hostname or "").lower()


def derive_purposes(meta: dict[str, Any]) -> list[str]:
    """Purposes a credential's own metadata declares or plainly implies.

    A ``git_host`` is a declaration of ``git``. Everything else comes from the
    notes, and a credential whose notes say nothing gets an empty list, not a
    guess.
    """
    found: list[str] = []
    if str(meta.get("git_host") or "").strip():
        found.append("git")
    haystack = f" {str(meta.get('notes') or '').lower()} "
    for purpose, needles in PURPOSE_PATTERNS.items():
        if purpose not in found and any(n in haystack for n in needles):
            found.append(purpose)
    return found


def parse_when(raw: Any) -> datetime | None:
    """An ISO date or datetime as an aware datetime; None when it is not one."""
    text = str(raw or "").strip()
    if not text:
        return None
    for parse in (datetime.fromisoformat, lambda t: datetime.strptime(t, "%Y-%m-%d")):
        try:
            when = parse(text)
        except ValueError:
            continue
        return when if when.tzinfo else when.replace(tzinfo=UTC)
    return None


def derive_health(
    meta: dict[str, Any], *, now: datetime | None = None, within_days: int = DEFAULT_WITHIN_DAYS
) -> str:
    """``ok``, ``expiring``, ``expired``, ``no_expiry`` or ``unparseable``.

    ``no_expiry`` is not ``ok``: nothing can ever flag a credential without a
    recorded expiry, which is how one expired unnoticed in the first place.
    """
    raw = meta.get("expires_at")
    if not str(raw or "").strip():
        return "no_expiry"
    when = parse_when(raw)
    if when is None:
        return "unparseable"
    current = now or datetime.now(UTC)
    if when <= current:
        return "expired"
    return "expiring" if when <= current + timedelta(days=within_days) else "ok"


def row_of(
    meta: dict[str, Any], *, now: datetime | None = None, within_days: int = DEFAULT_WITHIN_DAYS
) -> dict[str, Any]:
    """One index row: the recorded metadata plus the three derived fields."""
    row = dict(meta)
    row["host"] = derive_host(meta)
    row["purposes"] = derive_purposes(meta)
    row["health"] = derive_health(meta, now=now, within_days=within_days)
    return row


# --------------------------------------------------------------------------- the index


@dataclass
class CredentialIndex:
    """The single read model over the credential metadata.

    ``store`` is a ``ForeignCredentialStore`` (or anything with ``list()``);
    by default the one for ``state_dir``.
    """

    state_dir: Path | None = None
    store: Any = None
    now: datetime | None = None
    within_days: int = DEFAULT_WITHIN_DAYS
    _rows: list[dict[str, Any]] | None = field(default=None, repr=False)

    def _store(self):
        if self.store is None:
            from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

            if self.state_dir is None:
                from axiom.infra.paths import get_user_state_dir

                self.state_dir = get_user_state_dir()
            self.store = ForeignCredentialStore(Path(self.state_dir))
        return self.store

    def rows(self) -> list[dict[str, Any]]:
        """Every credential, sorted by name, each with host, purposes and health."""
        if self._rows is None:
            self._rows = [
                row_of(m, now=self.now, within_days=self.within_days)
                for m in self._store().list()
            ]
            self._rows.sort(key=lambda r: str(r.get("name", "")))
        return [dict(r) for r in self._rows]

    def for_host(self, host: str) -> list[dict[str, Any]]:
        """The rows whose derived host is ``host``. ``resolve`` and ``list --host`` both call this."""
        wanted = str(host or "").strip().lower()
        if "://" in wanted:
            wanted = (urlsplit(wanted).hostname or wanted).lower()
        return [r for r in self.rows() if r["host"] == wanted]

    def hosts(self) -> list[str]:
        return sorted({r["host"] for r in self.rows() if r["host"]})

    def without_host(self) -> list[str]:
        """Names no host can be derived for. resolve names them, so a miss is explained."""
        return [str(r.get("name", "")) for r in self.rows() if not r["host"]]


# --------------------------------------------------------------------------- migration


@dataclass(frozen=True)
class MigrationReport:
    path: str
    from_version: int | None
    to_version: int
    changed: bool
    hosts_recorded: tuple[str, ...] = ()
    backup: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "changed": self.changed,
            "hosts_recorded": list(self.hosts_recorded),
            "backup": self.backup,
        }


def upgrade_document(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Version 2 of an index document, and the names that gained a ``host``.

    Pure and additive: every field present is kept as it is. A record gains
    ``host`` only when one can be derived and it has none.
    """
    out = json.loads(json.dumps(data))  # a deep copy, without sharing a dict with the caller
    creds = out.setdefault("credentials", {})
    gained: list[str] = []
    for name in sorted(creds):
        record = creds[name]
        if not isinstance(record, dict) or str(record.get("host") or "").strip():
            continue
        host = derive_host(record)
        if host:
            record["host"] = host
            gained.append(name)
    out["version"] = max(int(out.get("version") or 1), INDEX_VERSION)
    return out, gained


def _v1_copy_path(path: Path) -> Path:
    return path.with_name("foreign-credentials.v1.json")


def keep_v1_copy(path: Path) -> Path | None:
    """Copy a version 1 index aside, once, before anything rewrites it.

    Returns the copy's path when one exists (made now or earlier), else None.
    A file that is already version 2, missing or unreadable is left alone: the
    store's own reader reports damage, and a copy of damage helps nobody.
    """
    from axiom.infra.state_integrity import atomic_write_text

    path = Path(path)
    copy = _v1_copy_path(path)
    if copy.exists():
        return copy
    try:
        text = path.read_text(encoding="utf-8")
        version = int((json.loads(text) or {}).get("version") or 1)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if version >= INDEX_VERSION:
        return None
    atomic_write_text(copy, text, mode=0o600)
    return copy


def migrate(state_dir: Path, *, dry_run: bool = False) -> MigrationReport:
    """Bring the on-disk index to version 2. Idempotent, lossless, metadata only.

    Before the first rewrite, the version 1 file is copied once to
    ``foreign-credentials.v1.json`` beside it. Running this again on a version 2
    file changes nothing and writes nothing.
    """
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.infra.state_integrity import atomic_write_text

    store = ForeignCredentialStore(Path(state_dir))
    path = store.index_path
    if not path.exists():
        return MigrationReport(str(path), None, INDEX_VERSION, changed=False)
    data = store._load_index()  # raises on a damaged file; damaged is not empty
    was = int(data.get("version") or 1)
    upgraded, gained = upgrade_document(data)
    if was >= INDEX_VERSION and not gained:
        return MigrationReport(str(path), was, was, changed=False)
    if dry_run:
        return MigrationReport(
            str(path), was, INDEX_VERSION, changed=True, hosts_recorded=tuple(gained),
            backup=str(_v1_copy_path(path)) if was < INDEX_VERSION else "",
        )
    backup = keep_v1_copy(path)
    atomic_write_text(path, json.dumps(upgraded, indent=2, sort_keys=True), mode=0o600)
    return MigrationReport(
        str(path), was, INDEX_VERSION, changed=True, hosts_recorded=tuple(gained),
        backup=str(backup) if backup else "",
    )


__all__ = [
    "DEFAULT_WITHIN_DAYS",
    "INDEX_VERSION",
    "PURPOSE_PATTERNS",
    "CredentialIndex",
    "MigrationReport",
    "derive_health",
    "derive_host",
    "derive_purposes",
    "keep_v1_copy",
    "migrate",
    "parse_when",
    "row_of",
    "upgrade_document",
]
