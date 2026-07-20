# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""HERALD ``send()`` façade.

Per spec-axiom-notifications §7: the public send API. SEC-1 wires the
classification-routing site (§4) + the inbox adapter dispatch (§9) + the
delivery-receipt write (§8). HERALD-2 adds real channel adapters; HERALD-3
adds GUARD + KEEP integration (the call sites are stubbed below with
TODOs).

Sync, not async, in SEC-1 — only the inbox adapter ships and it's
synchronous. HERALD-2 lifts to async per spec §7's signature.
"""

from __future__ import annotations

import inspect
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelAdapterRegistry,
)
from axiom.extensions.builtins.notifications.channels.inbox import (
    InboxChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.inbox import (
    InboxStore,
    InMemoryInboxStore,
)
from axiom.extensions.builtins.notifications.owner_resolution import (
    resolve_owner_display,
)
from axiom.extensions.builtins.notifications.sender import (
    SenderIdentity,
    SenderRegistry,
)
from axiom.governance import Classification


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


@dataclass(frozen=True)
class NotificationPayload:
    summary: str
    body: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeliveryReceipt:
    """Spec §8 row shape; SEC-1 keeps it in-memory + returns directly."""

    id: str
    actor: str
    recipient: str
    intent: str
    classification: Classification
    priority: Priority
    channel_selected: str | None
    outcome: str  # pending|succeeded|failed|denied|expired
    routing_rationale: list[dict[str, Any]] = field(default_factory=list)
    correlation_id: str = ""
    latency_ms: int | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class SendContext:
    """Per-deployment context. Production wires real adapters + the
    KEEP capability store; SEC-1 tests use defaults."""

    registry: ChannelAdapterRegistry = field(default_factory=ChannelAdapterRegistry)
    inbox_store: InboxStore = field(default_factory=InMemoryInboxStore)
    sender_registry: SenderRegistry | None = None
    receipts: dict[str, DeliveryReceipt] = field(default_factory=dict)
    dedup_log: dict[tuple[str, str], str] = field(default_factory=dict)
    """``(actor, dedup_key) → receipt_id``; per fabric §6.1 sliding window."""
    channel_configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Per-channel dispatch config (resolved secrets + non-secret bits),
    threaded into ``provider.build(cfg)`` at dispatch. ``inbox`` is handled
    separately (it takes the live ``inbox_store``); any external channel
    without an entry here is treated as UNCONFIGURED and skipped
    fail-closed (→ inbox). Populated by ``channel_config.rehydrate_from_env``
    or by the caller/tests."""

    @classmethod
    def default(
        cls,
        *,
        rehydrate: bool = True,
        env: Any | None = None,
    ) -> SendContext:
        """Build the process default context.

        Always registers the inbox adapter (the always-available baseline).
        When ``rehydrate`` is true (the default for ``axi notifications
        send``), also rehydrates any EXTERNAL channels that are fully
        configured in the environment — this is what lets a resolved
        non-inbox channel actually dispatch. Rehydration is fail-closed:
        an unconfigured / partially-configured channel is simply not
        registered, so the send falls back to the inbox channel.
        """
        ctx = cls()
        ctx.registry.register(InboxChannelAdapterProvider(store=ctx.inbox_store))
        if rehydrate:
            from axiom.extensions.builtins.notifications.channel_config import (
                rehydrate_from_env,
            )

            rehydrate_from_env(ctx, env=env)
        return ctx


@dataclass
class ChannelPreferences:
    """Per-(class × priority) ordered channel list per spec §8."""

    ordered_channels: tuple[str, ...] = ("inbox",)


def _mint_correlation_id() -> str:
    return f"corr-{uuid.uuid4().hex}"


_PRINCIPAL_RE = re.compile(r"^@([A-Za-z0-9_\-.]+)(?::([A-Za-z0-9_\-.]+))?$")


def resolve_sender(actor: str, ctx: SendContext) -> SenderIdentity:
    """Build the servant SenderIdentity for ``actor`` (ADR-066).

    Looks up the registered identity when available, then fills the
    possessive ``{owner}`` from the inherent node identity: settings
    ``user.name`` for the local context, peer registry for remote owners,
    birth-host as the always-present fallback (see owner_resolution).
    """
    m = _PRINCIPAL_RE.match(actor or "")
    name = m.group(1) if m else (actor or "agent").lstrip("@")
    context = m.group(2) if m else None

    base: SenderIdentity | None = None
    if ctx.sender_registry is not None:
        try:
            base = ctx.sender_registry.get(actor)
        except KeyError:
            base = None

    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        settings = SettingsStore()
    except Exception:
        settings = None

    local_context = None
    if settings is not None:
        dm = _PRINCIPAL_RE.match(settings.get("memory.default_principal", "") or "")
        if dm:
            local_context = dm.group(2)

    owner = resolve_owner_display(
        context, local_context=local_context, settings=settings, peers=None
    )
    if base is not None:
        return SenderIdentity(
            principal=base.principal,
            display_name=base.display_name,
            version=base.version,
            owner_handle=owner,
            avatar_uri=base.avatar_uri,
            from_address=base.from_address,
        )
    return SenderIdentity(
        principal=actor, display_name=name.upper(), version="", owner_handle=owner
    )


def _resolve_channel_config(name: str, ctx: SendContext) -> dict[str, Any] | None:
    """Resolve the build-time config for channel ``name``.

    ``inbox`` always resolves to the live inbox store. Every other channel
    must have an entry in ``ctx.channel_configs`` (populated by
    ``channel_config.rehydrate_from_env`` from resolved secrets); a channel
    with no entry is UNCONFIGURED and returns ``None`` so the dispatcher
    fails closed and falls through to the inbox.
    """
    if name == "inbox":
        return {"store": ctx.inbox_store}
    cfg = ctx.channel_configs.get(name)
    if not cfg:
        return None
    return dict(cfg)


def _deliver_via(
    adapter: Any,
    *,
    recipient: str,
    receipt_id: str,
    classification: Classification,
    priority: Priority,
    payload: NotificationPayload,
    sender: SenderIdentity,
) -> Any:
    """Call ``adapter.deliver_sync`` with the uniform kwargs.

    Every channel adapter (inbox, slack, twilio-sms, aws-sns-sms, acs-sms,
    email, fcm-push, …) exposes ``deliver_sync(**common)`` and returns a
    result object carrying ``.ok`` + ``.error``. The email adapter
    additionally accepts a rich ``body_text``; it is passed only when the
    adapter's signature accepts it, keeping the call uniform across
    channels.
    """
    kwargs: dict[str, Any] = dict(
        recipient=recipient,
        receipt_id=receipt_id,
        classification=classification,
        priority=priority.value,
        summary=payload.summary,
        sender=sender,
    )
    try:
        params = inspect.signature(adapter.deliver_sync).parameters
    except (TypeError, ValueError):
        params = {}
    if "body_text" in params and payload.body:
        kwargs["body_text"] = payload.body
    return adapter.deliver_sync(**kwargs)


def send(
    ctx: SendContext,
    *,
    actor: str,
    recipient: str,
    payload: NotificationPayload,
    classification: Classification,
    priority: Priority = Priority.NORMAL,
    intent: str = "notification.send",
    channel_prefs: ChannelPreferences | None = None,
    dedup_key: str | None = None,
) -> DeliveryReceipt:
    """Dispatch a notification through admitted channels.

    Flow:

    1. Dedup check (fabric §6.1).
    2. Classification routing (spec §4) — ``registry.admitted_for()``. This
       is the compliance-load-bearing filter: an external (INTERNAL-ceiling)
       channel is NEVER a candidate for a ``regulated`` / ``controlled``
       (EC-controlled / ITAR) envelope.
    3. (TODO HERALD-3) GUARD authz.decide(envelope).
    4. (TODO HERALD-3) KEEP vault.get_capability per admitted channel.
    5. Adapter dispatch — build each admitted channel with its OWN resolved
       config/secrets and call ``deliver_sync``; the first success wins,
       with the always-available inbox as a fail-closed terminal fallback.
    6. Write delivery receipt + inbox rows.

    Real ActionEnvelope construction is deferred to HERALD-3 once GUARD
    + KEEP are wired; the intent + classification are the load-bearing
    fields today.
    """
    # 1. Dedup
    if dedup_key is not None:
        key = (actor, dedup_key)
        if key in ctx.dedup_log:
            return ctx.receipts[ctx.dedup_log[key]]

    receipt_id = f"rcpt-{uuid.uuid4().hex[:12]}"
    correlation_id = _mint_correlation_id()
    started = datetime.now(UTC)

    # 2. Classification routing
    candidates = ctx.registry.admitted_for(classification)
    rationale: list[dict[str, Any]] = []
    for p in ctx.registry.all():
        caps = p.capabilities()
        admitted = p in candidates
        rationale.append({
            "adapter": p.name,
            "admitted": admitted,
            "ceiling": caps.classification_ceiling.value,
            "envelope_classification": classification.value,
        })

    if not candidates:
        receipt = DeliveryReceipt(
            id=receipt_id,
            actor=actor,
            recipient=recipient,
            intent=intent,
            classification=classification,
            priority=priority,
            channel_selected=None,
            outcome="denied",
            routing_rationale=rationale,
            correlation_id=correlation_id,
            error="no_channel_at_or_below_classification",
        )
        ctx.receipts[receipt_id] = receipt
        if dedup_key is not None:
            ctx.dedup_log[(actor, dedup_key)] = receipt_id
        return receipt

    # Channel preference ordering. When the caller didn't pass an
    # explicit list, consult the recipient-preferences primitive: a
    # ``@handle`` may resolve to ``[slack:#alerts, twilio-sms:+1xxx,
    # email:ben@…, inbox]`` so one send fans out across the operator's
    # preferred channels. Falls back to all admitted candidates when
    # the recipient has no profile registered.
    prefs = channel_prefs
    if prefs is None:
        from axiom.extensions.builtins.notifications.preferences import (
            default_store,
            resolve_recipient,
        )

        profile = default_store().get(recipient)
        if profile is not None:
            prefs = resolve_recipient(
                profile, classification, priority, ctx.registry
            )
    if prefs is None:
        prefs = ChannelPreferences(
            ordered_channels=tuple(p.name for p in candidates)
        )

    # Build the ordered list of channels to ATTEMPT. Every name here is
    # already ceiling-admitted (it came from ``candidates``), so the
    # classification ceiling — the compliance-load-bearing filter — has
    # already excluded any external channel that must not carry this
    # envelope. ``prefs.ordered_channels`` sets the preference order.
    admitted_by_name = {p.name: p for p in candidates}
    attempt_names: list[str] = []
    for name in prefs.ordered_channels:
        if name in admitted_by_name and name not in attempt_names:
            attempt_names.append(name)

    # FAIL-CLOSED: ``inbox`` is the always-available last resort. Its
    # ceiling is CONTROLLED so it admits every tier — meaning an
    # ``ec-controlled`` / ``itar`` envelope (which no INTERNAL-ceiling
    # external channel is admitted for) still lands somewhere durable, and
    # an external channel that is unconfigured or fails to deliver falls
    # through to it rather than dropping the alert. It is appended LAST so
    # a configured, admitted external channel is still preferred.
    if "inbox" in admitted_by_name and "inbox" not in attempt_names:
        attempt_names.append("inbox")
    if not attempt_names:
        # No preference matched an admitted channel; fall back to the first
        # admitted candidate (still ceiling-filtered — never above the
        # envelope classification).
        attempt_names.append(candidates[0].name)

    # 5. Adapter dispatch — walk the admitted channels in order; the first
    #    successful delivery wins. Each adapter is built with ITS OWN
    #    resolved config (secrets included) — never a hardcoded inbox config.
    sender = resolve_sender(actor, ctx)
    outcome = "failed"
    error: str | None = None
    channel_selected: str | None = None
    for name in attempt_names:
        provider = admitted_by_name[name]
        cfg = _resolve_channel_config(name, ctx)
        if cfg is None:
            # Registered but not configured (no secret/config resolved).
            # Fail closed: skip and fall through to the next channel
            # (ultimately the always-available inbox).
            error = f"channel {name!r} not configured; skipped"
            continue
        try:
            adapter = provider.build(cfg)
        except Exception as exc:  # noqa: BLE001 — build/secret-resolution boundary
            error = f"channel {name!r} build failed: {type(exc).__name__}"
            continue
        result = _deliver_via(
            adapter,
            recipient=recipient,
            receipt_id=receipt_id,
            classification=classification,
            priority=priority,
            payload=payload,
            sender=sender,
        )
        if getattr(result, "ok", False):
            outcome = "succeeded"
            channel_selected = name
            error = None
            break
        error = getattr(result, "error", None) or f"channel {name!r} delivery failed"

    latency_ms = int(
        (datetime.now(UTC) - started).total_seconds() * 1000
    )
    receipt = DeliveryReceipt(
        id=receipt_id,
        actor=actor,
        recipient=recipient,
        intent=intent,
        classification=classification,
        priority=priority,
        channel_selected=channel_selected,
        outcome=outcome,
        routing_rationale=rationale,
        correlation_id=correlation_id,
        latency_ms=latency_ms,
        error=error,
    )
    ctx.receipts[receipt_id] = receipt
    if dedup_key is not None:
        ctx.dedup_log[(actor, dedup_key)] = receipt_id
    return receipt


__all__ = [
    "ChannelPreferences",
    "DeliveryReceipt",
    "NotificationPayload",
    "Priority",
    "SendContext",
    "send",
]
