# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``flags.stale`` and ``flags.stale_notify``: flags that are work to remove (LOOP-F11).

``flags.stale`` is the report on every surface (``flags stale``, ``axiom_flags__stale``,
``GET /api/v1/flags/stale``), read-only, for site admins and for the node's maintainers
(setting ``flags.maintainers``). One precondition decides on every surface
(:data:`STALE_READERS`). ``flags.stale_notify`` is what the weekly schedule runs: one grouped
message to each configured maintainer, never to a site admin, and none when no maintainer is
configured, behind the ``flag_review_notices`` flag (default off).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime
from typing import Any

from axiom.infra.skill_declarations import Precondition
from axiom.infra.skills import SkillContext, SkillResult

from .. import lifecycle

NOTICES_FLAG = "flag_review_notices"
_log = logging.getLogger("axiom.flags.stale")

#: The roles of a caller the web face has already authenticated (its session), set by the
#: route around the call. Server code sets it; a parameter cannot.
_session_roles: ContextVar[tuple[str, ...] | None] = ContextVar("axiom_flags_session_roles", default=None)


@contextmanager
def session_roles(roles: Iterable[str]) -> Iterator[None]:
    token = _session_roles.set(tuple(str(r) for r in roles))
    try:
        yield
    finally:
        _session_roles.reset(token)


def is_site_admin(roles: Iterable[str]) -> bool:
    from axiom.governance.roles import SITE_ADMIN, canonical_roles

    return SITE_ADMIN in canonical_roles(list(roles))


def caller_may_read_stale(params: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    """The node's own shell, a site admin (read-only) or one of the node's maintainers.

    A site admin is read from the web session, else from the directory (ADR-103)."""
    actor = getattr(ctx, "actor", None)
    if getattr(ctx, "surface", None) == "cli" and actor in (None, "", "@cli:local"):
        return True, "the node's own shell"
    if lifecycle.is_maintainer(actor or "", lifecycle.configured_maintainers()):
        return True, "maintainer"
    roles = _session_roles.get()
    if roles is None:
        from axiom.extensions.builtins.features.approvals import roles_of

        try:
            roles = roles_of(actor or "")
        except Exception as exc:  # noqa: BLE001 - an unreadable directory grants nothing
            return False, (f"the caller's roles could not be read ({exc}); "
                           "the stale-flag report is for site admins and maintainers")
    if is_site_admin(roles):
        return True, "site admin"
    return False, f"{actor or 'this caller'} is not a site admin or a maintainer; the stale-flag report is theirs"


STALE_READERS = Precondition(
    id="stale_readers",
    description="The caller is a site admin (read-only) or one of this node's maintainers",
    check=caller_may_read_stale,
    fix="ask a site admin or a maintainer, or add yourself to the flags.maintainers setting",
)


def _today(params: dict[str, Any]) -> date:
    raw = str(params.get("today") or "").strip()
    return date.fromisoformat(raw) if raw else datetime.now(UTC).date()


def _record(environment: str) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], str]:
    """Rollouts and overrides from the record, or nothing and why."""
    try:
        from axiom.extensions.builtins.flags.store import OverrideStore, TargetingStore

        rollouts = TargetingStore().rollouts(environment)
        overrides = OverrideStore().rows()
        return rollouts, overrides, "read"
    except Exception as exc:  # noqa: BLE001 - no database: the registry and the environment still answer
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return {}, [], f"unread ({first[:160]}): rollouts and overrides were not considered"


def build_report(params: dict[str, Any]) -> dict[str, Any]:
    from axiom.extensions.builtins.features.flag_registry import get_environment, registry
    from axiom.extensions.builtins.features.resolver import env_override

    flags = registry()
    environment = str(params.get("environment") or "").strip().lower() or get_environment()
    roots = [str(r) for r in params.get("roots") or []] or lifecycle.default_roots()
    rollouts, overrides, record = _record(environment)
    refs = lifecycle.find_references(flags, roots)
    pins = {k: v for k in flags if (v := env_override(k)) is not None}
    report = lifecycle.stale_report(flags, environment=environment, today=_today(params), rollouts=rollouts,
                                    overrides=overrides, referenced=lambda k: bool(refs.get(k)), env_pins=pins,
                                    maintainers=lifecycle.configured_maintainers())
    report["record"] = record
    report["searched"] = roots
    return report


def stale(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    try:
        report = build_report(params)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value=report)


def _notices_on() -> bool:
    from axiom.extensions.builtins.features.resolver import env_override

    pinned = env_override(NOTICES_FLAG)
    if pinned is not None:
        return pinned == "on"
    try:
        from axiom.extensions.builtins.flags.service import FlagService

        return FlagService().is_enabled(NOTICES_FLAG, node=os.environ.get("AXIOM_NODE") or None)
    except Exception:  # noqa: BLE001 - unreadable: the default (off)
        return False


def herald_sender(recipient: str, summary: str, body: str, dedup: str) -> str | None:
    try:
        from axiom.extensions.builtins.notifications.send import NotificationPayload, Priority, send
        from axiom.extensions.builtins.notifications.skills.send import _ctx
        from axiom.governance import Classification

        receipt = send(
            _ctx(), actor="@rivet", recipient=recipient,
            payload=NotificationPayload(summary=summary, body=body, metadata={"kind": "flags_stale"}),
            classification=Classification.INTERNAL, priority=Priority.NORMAL,
            intent="flags.stale", dedup_key=dedup,
        )
        return receipt.id
    except Exception as exc:  # noqa: BLE001 - a failed send is reported, never raised
        _log.warning("stale-flag notice to %s failed: %s", recipient, exc)
        return None


def stale_notify(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    if not _notices_on():
        return SkillResult(ok=True, value={"sent": [], "why": f"the {NOTICES_FLAG} flag is off"})
    try:
        report = build_report(params)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if not report["maintainers"]:
        return SkillResult(ok=True, value={"sent": [], "why": lifecycle.NO_MAINTAINER_NOTE, "report": report})
    if not report["stale"]:
        return SkillResult(ok=True, value={"sent": [], "why": "no flag is work to remove", "report": report})
    sender = params.get("_sender") or herald_sender
    year, week, _ = _today(params).isocalendar()
    summary, body = lifecycle.message_for(report["stale"], environment=report["environment"])
    sent = []
    for recipient in report["maintainers"]:
        receipt = sender(recipient, summary, body, f"flags.stale:{recipient}:{year}-W{week:02d}")
        sent.append({"recipient": recipient, "flags": [i["key"] for i in report["stale"]],
                     "receipt": receipt, "sent": bool(receipt)})
    return SkillResult(ok=True, value={"sent": sent, "report": report},
                       actions_taken=[f"sent {sum(s['sent'] for s in sent)} grouped notice(s) to "
                                      f"{len(sent)} maintainer(s) about {len(report['stale'])} stale flag(s)"])


__all__ = ["NOTICES_FLAG", "build_report", "herald_sender", "stale", "stale_notify"]
