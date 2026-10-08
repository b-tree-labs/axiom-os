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
      - ``recipient`` (str, optional): who it reaches. When absent, the
        declared audience is used (``receipts.audience``) — which is how a
        scheduled run works at all, since a manifest-declared schedule
        carries no params. With neither, the refusal names the file to
        write rather than only saying something is missing.
      - ``period`` (str, optional): the cadence period this run belongs to,
        which is what suppression is scoped by. A scheduler passes its own
        fire bucket. The default is the current UTC minute, so re-running
        the verb by hand does not interrupt somebody twice while no
        plausible cadence is suppressed.
      - ``dry_run`` (bool): compose and return it without sending, which
        is how you look at what WOULD arrive before arming a schedule.
    """
    from datetime import UTC, datetime

    from axiom.extensions.builtins.fleet import store as fleet_store

    from .. import store
    from ..audience import load_audience, no_audience_declared, validate_audience
    from ..brief import compose_brief
    from ..digest import compose_digest
    from ..sources import all_claims

    dry_run = bool(params.get("dry_run"))
    period = str(params.get("period") or "").strip() or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")

    # An explicit recipient is a one-off and wins. Otherwise the declared
    # audience answers it, because a scheduled run has no params to carry
    # one and the question "who is arrival for" is deployment state.
    asked = str(params.get("recipient") or "").strip()
    declared = load_audience()
    recipients: tuple[str, ...] = (asked,) if asked else ()
    site = params.get("site") or None
    where = str(params.get("where") or "")

    if not asked and declared is not None:
        problems = validate_audience(declared)
        if problems:
            # A malformed declaration is not "nobody declared one". Say
            # which, and say every problem at once.
            return SkillResult(ok=False, errors=problems)
        if not declared.enabled:
            return SkillResult(
                ok=True,
                value={"sent": False, "durable": False, "outcome": "disabled"},
                actions_taken=["the declared audience is disabled; nothing was sent"],
            )
        recipients = declared.recipients
        site = site or (declared.site or None)
        where = where or declared.where

    if not recipients and not dry_run:
        return SkillResult(ok=False, errors=[no_audience_declared()])

    sites = [site] if site else None
    with fleet_store.session_scope() as fsession:
        items = all_claims(fsession, sites=sites).as_items()
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

    # One send per recipient, so suppression stays per-person and one
    # undeliverable address does not silence everybody else's digest.
    deliveries = [_deliver(digest, recipient=who, period=period, ctx=ctx) for who in recipients]
    return SkillResult(
        ok=True,
        value={
            "digest": digest.payload(),
            "deliveries": {who: d for who, d in zip(recipients, deliveries, strict=True)},
            # `sent` is the fleet-wide answer to "did this reach anybody",
            # which is what a scheduled run's caller acts on. Per-recipient
            # truth is in `deliveries` — rolling up with `all()` would let
            # one bad address report a total failure.
            "sent": any(d["sent"] for d in deliveries),
            "durable": any(d["durable"] for d in deliveries),
        },
        actions_taken=[d["detail"] for d in deliveries],
    )


def _this_node() -> str:
    import os
    import platform

    return os.environ.get("AXIOM_FLEET_NODE_ID") or platform.node()


def _send(digest, *, recipient: str, actor: str, dedup_key: str):
    """The one call out to the notification fabric. A seam, so a test can
    stand in a receipt without a channel and without patching the fabric."""
    from axiom.extensions.builtins.notifications.send import (
        NotificationPayload,
        SendContext,
        send,
    )
    from axiom.governance.classification import Classification

    return send(
        # SendContext.default(), NOT SendContext(). The bare constructor
        # registers no adapters at all, so every send from it came back
        # `denied / no_channel_at_or_below_classification` — nothing
        # reached anybody, on every run since this shipped. `default()`
        # registers the inbox baseline and rehydrates whatever external
        # channels the environment has fully configured, fail-closed.
        SendContext.default(),
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
        dedup_key=dedup_key,
    )


def _dedup_key(*, recipient: str, period: str) -> str:
    """One interruption per recipient per cadence period.

    The key used to be the digest's SUBJECT, which is stable whenever the
    case count is — and ``SendContext.default()`` keeps a durable dedup log.
    So the second digest reading "2 cases waiting on you" was suppressed
    for ever: a site whose situation did not change got exactly one message,
    which is the precise failure mode "it sends on the cadence even when
    quiet" exists to prevent. The subject also omitted the recipient, so a
    digest to a second person was suppressed as a duplicate of the first
    person's.

    Both are the same mistake — keying suppression on what the message
    SAYS rather than on the interruption it IS.
    """
    return f"receipts.digest:{recipient}:{period}"


def _deliver(digest, *, recipient: str, period: str, ctx: SkillContext | None) -> dict:
    """Hand the digest to the notification fabric and report what happened.

    Never raises: a digest that could not be delivered is a fact worth
    returning, not an exception worth crashing a scheduled run over.

    **It believes the receipt.** This used to return True whenever ``send``
    had not raised, which is the whole bug: the fabric was answering
    ``denied / no_channel_at_or_below_classification`` and the verb
    reported ``sent: true`` regardless. Arrival is the one path whose
    entire job is to reach a person, so a false green there means a site
    believes it is being told things nobody is telling it.

    Two fields, because the fabric distinguishes two things and so must we:

    - ``sent`` — a person can still read this.
    - ``durable`` — it survived the process. The default inbox store is
      in-memory and accepts everything while keeping none of it, and a
      scheduled run is a fresh process every time, so accepted-but-not-
      durable means lost. That is reported, not rounded up.

    Making the inbox itself durable is a separate change: an inbox row has
    a NOT NULL foreign key to ``delivery_receipts`` and nothing persists
    receipts yet. Until then this says so rather than implying delivery.
    """
    from axiom.infra.skills import ensure_context

    the_ctx = ensure_context(ctx)
    actor = getattr(getattr(the_ctx, "principal", None), "handle", "") or "@receipts:local"

    try:
        receipt = _send(
            digest,
            recipient=recipient,
            actor=actor,
            dedup_key=_dedup_key(recipient=recipient, period=period),
        )
    except Exception as exc:  # noqa: BLE001 — see the docstring
        return {
            "sent": False,
            "durable": False,
            "outcome": "error",
            "detail": f"could not send the digest to {recipient}: {type(exc).__name__}",
        }

    outcome = str(getattr(receipt, "outcome", "") or "unknown")
    durable = bool(getattr(receipt, "durable", False))
    error = getattr(receipt, "error", None)
    # The fabric does not always name the channel it used — a successful
    # inbox send comes back with channel=None. Say "it" rather than
    # inventing a name or printing "no channel" over a send that worked.
    named = getattr(receipt, "channel", None)
    via = f" via {named}" if named else ""
    who = f"{named} accepted" if named else "the digest was accepted for"

    if outcome == "suppressed_duplicate":
        # Not a failure: the fabric declining to interrupt somebody twice
        # is the fabric working. Reporting it as undelivered would make a
        # correctly-quiet scheduled run look like an outage, and whoever
        # read that would go looking for a broken channel.
        return {
            "sent": False,
            "durable": False,
            "outcome": outcome,
            "detail": (
                f"an identical digest already went to {recipient} this period; "
                "not interrupting them again"
            ),
        }

    if outcome != "succeeded":
        reason = f": {error}" if error else ""
        return {
            "sent": False,
            "durable": False,
            "outcome": outcome,
            "detail": f"the digest was not delivered to {recipient} — {outcome}{reason}",
        }

    if not durable:
        return {
            "sent": False,
            "durable": False,
            "outcome": outcome,
            "detail": (
                f"{who} {recipient} but it will not survive this process, so "
                "nobody can read it — treating that as undelivered"
            ),
        }

    return {
        "sent": True,
        "durable": True,
        "outcome": outcome,
        "detail": f"sent the digest to {recipient}{via}: {digest.waiting} waiting",
    }


__all__ = ["run"]
