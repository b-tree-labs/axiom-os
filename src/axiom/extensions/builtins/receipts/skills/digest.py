# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``receipts.digest`` — send the arrival message.

A verb rather than a background thread, for the reasons ADR-056 gives
and one more: arrival is a side effect on somebody's attention, so it
goes through the gateway like every other side effect — GUARD consult,
site rules, audit chain. A digest that could be sent privately would be
the one capability in this extension nobody could account for.

It rides the schedule substrate: the scheduler's executor invokes a
qualified skill name on a cadence, so arming the digest is arming a
schedule and needs no machinery of its own.

Delivery reuses ``notifications.send``, which already carries the part
that matters and is easy to get wrong — classification routing, so a
controlled envelope is never a candidate for an external channel; dedup;
and a fail-closed inbox when no channel is admitted. Building a second
sender here would be building a second thing to get that wrong in.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Compose the current brief and send it.

    Params:
      - ``site`` (str, optional): scope, as the API route scopes.
      - ``where`` (str, optional): the link to the surface. Omitted
        rather than faked — a digest pointing somewhere wrong is worse
        than one pointing nowhere.
      - ``recipient`` (str): who it reaches.
      - ``dry_run`` (bool): compose and return it without sending, which
        is how you look at what WOULD arrive before arming a schedule.
    """
    from axiom.extensions.builtins.fleet import store as fleet_store

    from .. import store
    from ..brief import compose_brief, fleet_source
    from ..digest import compose_digest

    site = params.get("site") or None
    where = str(params.get("where") or "")
    recipient = str(params.get("recipient") or "").strip()
    dry_run = bool(params.get("dry_run"))

    if not recipient and not dry_run:
        return SkillResult(
            ok=False,
            errors=["recipient is required — a digest with nobody to reach is not a send"],
        )

    sites = [site] if site else None
    with fleet_store.session_scope() as fsession:
        items = fleet_source(fsession, sites=sites)
        with store.session_scope() as session:
            # snapshot=False: composing the arrival message must not
            # advance the trust-delta baseline. Reading is not deciding,
            # and a digest that consumed deltas would make the next one
            # silently different for having been sent.
            brief = compose_brief(
                session,
                items,
                site=site,
                snapshot=False,
                this_node=_this_node(),
                fleet_session=fsession,
            )

    digest = compose_digest(brief, where=where)

    if dry_run:
        return SkillResult(
            ok=True,
            value={"digest": digest.payload(), "sent": False},
            actions_taken=["composed the digest; nothing was sent"],
        )

    sent = _deliver(digest, recipient=recipient, ctx=ctx)
    return SkillResult(
        ok=True,
        value={"digest": digest.payload(), "sent": sent},
        actions_taken=[
            f"sent the digest to {recipient}: {digest.waiting} waiting"
            if sent
            else f"could not send the digest to {recipient}"
        ],
    )


def _this_node() -> str:
    import os
    import platform

    return os.environ.get("AXIOM_FLEET_NODE_ID") or platform.node()


def _deliver(digest, *, recipient: str, ctx: SkillContext | None) -> bool:
    """Hand the digest to the notification fabric. Never raises.

    A digest that could not be delivered is a fact worth returning, not
    an exception worth crashing a scheduled run over — and the fabric
    already falls back to the inbox rather than dropping.
    """
    from axiom.infra.skills import ensure_context

    try:
        from axiom.extensions.builtins.notifications.send import (
            NotificationPayload,
            SendContext,
            send,
        )
        from axiom.governance.classification import Classification
    except Exception:  # noqa: BLE001 — the extension may not be installed
        return False

    the_ctx = ensure_context(ctx)
    actor = getattr(getattr(the_ctx, "principal", None), "handle", "") or "@receipts:local"
    try:
        send(
            SendContext(),
            actor=actor,
            recipient=recipient,
            payload=NotificationPayload(
                summary=digest.subject,
                body=digest.body,
                metadata={"waiting": digest.waiting, "needs_anyone": digest.needs_anyone},
            ),
            # The brief carries entity names and evidence sentences from
            # somebody's infrastructure. INTERNAL is the floor, and the
            # fabric refuses an external channel above its ceiling.
            classification=Classification.INTERNAL,
            intent="receipts.digest",
            # One digest per cadence per recipient. Re-running the verb
            # does not re-interrupt somebody.
            dedup_key=f"receipts.digest:{digest.subject}",
        )
    except Exception:  # noqa: BLE001 — see the docstring
        return False
    return True


__all__ = ["run"]
