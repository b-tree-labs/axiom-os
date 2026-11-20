# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a log claims to the oversight brief (unit X1): missed checks and waiting co-signatures.

One ``OversightItem`` per (site, log, claim kind), never one per miss, so a log with five missed
checks takes one line of the brief's attention budget, not five:

- ``attest_missed_check``, status ``failed``: a required obligation (``severity = alarm``) is
  overdue right now. Evidence: "<type> was due <time> and none was signed (<n> open)". An optional
  obligation (a notice) produces no item. It clears when the entry is signed, because the
  obligation's own ``evaluate`` reads the chain.
- ``attest_pending_cosign``, status ``stale``: statements whose type needs a co-signature have
  waited longer than their window. Evidence: "<n> entries have waited <age> for a co-signature".
  The window is the type's ``cosign.window``, else the site setting ``attest.cosign_window``
  (a duration such as ``1h``), else :data:`DEFAULT_COSIGN_WINDOW_SECONDS`.
- ``attest_integrity`` is reserved for chain-verification results (unit X2); :func:`integrity_items`
  returns nothing yet.

A read: it decides nothing, acts on nothing and carries no next action, because only a person may
sign or co-sign. The receipts extension reads it through one registered source per declared log
(``attest_<log>``, :func:`register_sources`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from . import obligations, registry, service, site_settings, store
from .logbooks import LogbookError, _duration

DEFAULT_COSIGN_WINDOW_SECONDS = 60 * 60
SITE_WINDOW_KEY = "attest.cosign_window"

_SITES = "SELECT DISTINCT site_id FROM attest_records WHERE logbook = :logbook"


def _when(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    from axiom.policy.action_ledger import parse_when

    return parse_when(str(value))


def _iso(at: datetime | None) -> str:
    return at.astimezone(UTC).isoformat() if at else ""


def _age(seconds: int) -> str:
    minutes = max(1, seconds // 60)
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} d"


def _sites_of(logbook_id: str, sites: list[str] | None) -> list[str]:
    if sites:
        return list(sites)
    with store.session_scope() as s:
        return [r[0] for r in s.execute(text(_SITES), {"logbook": logbook_id}).all()]


def _window(site: str, decl_seconds: int | None) -> int:
    if decl_seconds:
        return decl_seconds
    raw = site_settings.value(site, SITE_WINDOW_KEY, None)
    if raw:
        try:
            return _duration(raw, f"site {site} {SITE_WINDOW_KEY}", "window")
        except LogbookError:
            pass  # a malformed site value falls back; the default still names the wait
    return DEFAULT_COSIGN_WINDOW_SECONDS


def _missed(site: str, logbook_id: str, now: datetime) -> Any:
    from axiom.extensions.builtins.receipts.brief import OversightItem

    misses = [
        o
        for o in obligations.evaluate(site, logbook_id, now=now)
        if o["state"] == "missed" and o["severity"] == "alarm"
    ]
    if not misses:
        return None
    oldest = min(misses, key=lambda o: o["due_at"])
    due = _when(oldest["due_at"])
    return OversightItem(
        entity_kind="logbook",
        entity_id=logbook_id,
        claim_kind="attest_missed_check",
        status="failed",
        evidence=f"{oldest['type']} was due {due:%Y-%m-%d %H:%M} UTC and none was signed "
        f"({len(misses)} open)",
        site=site,
        observed_at=_iso(due),
    )


def _waiting(site: str, logbook_id: str, now: datetime) -> Any:
    from axiom.extensions.builtins.receipts.brief import OversightItem

    book = registry.get(logbook_id)
    windows = {
        t.id: _window(site, t.cosign.window_seconds) for t in book.types if t.cosign is not None
    }
    if not windows:
        return None
    pending = service.pending_cosigns(site, logbook_id, older_than=min(windows.values()), now=now)
    late = [p for p in pending if p["age_seconds"] >= windows.get(p.get("entry_type") or "", 0)]
    if not late:
        return None
    oldest = max(late, key=lambda p: p["age_seconds"])
    n = len(late)
    noun = "1 entry has" if n == 1 else f"{n} entries have"
    return OversightItem(
        entity_kind="logbook",
        entity_id=logbook_id,
        claim_kind="attest_pending_cosign",
        status="stale",
        evidence=f"{noun} waited {_age(int(oldest['age_seconds']))} for a co-signature",
        site=site,
        observed_at=_iso(_when(oldest.get("occurred_at"))),
    )


def read_logbook(logbook_id: str, *, sites: list[str] | None = None, now: datetime | None = None) -> list:
    """The claims one log makes at each site: at most one item per claim kind."""
    moment = now or datetime.now(UTC)
    out = []
    for site in _sites_of(logbook_id, sites):
        for item in (_missed(site, logbook_id, moment), _waiting(site, logbook_id, moment)):
            if item is not None:
                out.append(item)
    return out


def read_logbook_claims(*, sites: list[str] | None = None, now: datetime | None = None) -> list:
    """Every declared log's claims (what the brief reads, all logs at once)."""
    out = []
    for book in registry.all_logbooks():
        out.extend(read_logbook(book.id, sites=sites, now=now))
    return out


def integrity_items(*, sites: list[str] | None = None, now: datetime | None = None) -> list:
    """Reserved for chain-verification results (``attest_integrity``, unit X2). Nothing yet."""
    return []


def register_sources(register: Any, names: list[str]) -> None:
    """One source per declared log, ``attest_<log>``, skipping any already registered."""
    for book in registry.all_logbooks():
        name = f"attest_{book.id}"
        if name in names:
            continue

        def _read(*, sites=None, now=None, _id=book.id):
            return read_logbook(_id, sites=sites, now=now)

        register(name, f"the log {book.id}", _read)
        names.append(name)


__all__ = [
    "DEFAULT_COSIGN_WINDOW_SECONDS",
    "SITE_WINDOW_KEY",
    "integrity_items",
    "read_logbook",
    "read_logbook_claims",
    "register_sources",
]
