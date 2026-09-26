# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The principal *profile* — how to reach the human behind a handle.

P0 of ``spec-axiom-principal-comms.md`` §2.

**This is not a second Principal type.** Identity already has two layers:
``axiom.vega.identity.Principal`` (who an entity is, provably — handle plus
public key) and ``axiom.infra.principal.PrincipalContext`` (who is acting right
now, and at what assurance posture). Both key on the Matrix-style
``@name:context`` handle. This module adds the layer neither covers —
*reachability* — and keys on the **same handle** so a skill invocation that knows
it is acting as ``@ben:netl`` can look up how to reach ``@ben:netl``. Inventing a
separate identifier here would leave the reachability layer unable to join to the
identity layer above it.

The invariant this module carries is small and load-bearing: **an endpoint that
has not round-tripped is not a channel.** ``verified_at is None`` is what routing
consults, so a channel that is merely configured can never be selected. It is the
outbound mirror of ``PrincipalContext.assured``: configuration is not evidence in
either direction.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime

from axiom.vega.identity import parse_handle

__all__ = [
    "ChannelPreference",
    "ContactEndpoint",
    "EndpointHealth",
    "PrincipalKind",
    "PrincipalProfile",
    "PrincipalStatus",
]

# Kinds a P0 harness can already reach without new credentials.
ENDPOINT_KINDS = ("email", "chat", "sms", "inbox")


class PrincipalKind(enum.Enum):
    """What sort of entity this harness works for.

    ``vega.identity.Principal`` is already documented as "a named, public-keyed
    entity (human, agent, node, org)", so this records what the identity layer
    always permitted rather than widening it. Keying the profile on the handle
    (rather than on an invented identifier) is what makes the non-human case
    nearly free: ``@room-scheduler:site`` is a valid principal with no new scheme.

    The kind is a **capability discriminator, not a label**. A human is
    interviewed, has a personal voice, and confirms delivery by replying. Every
    other kind is provisioned declaratively, carries a service persona rather
    than a voice, and is verified by a machine round trip. Endpoints,
    preferences, routing and grants are identical across kinds.
    """

    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"
    NODE = "node"
    ORG = "org"

    @property
    def is_human(self) -> bool:
        return self is PrincipalKind.HUMAN


class PrincipalStatus(enum.Enum):
    PENDING = "pending"  # interview started, required answers missing
    UNVERIFIED = "unverified"  # answers complete, nothing has round-tripped
    ACTIVE = "active"  # at least one endpoint proven
    REVOKED = "revoked"


class EndpointHealth(enum.Enum):
    OK = "ok"
    DEGRADED = "degraded"  # previously proven, currently misbehaving
    FAILED = "failed"  # never proven, or proven-then-hard-failed
    UNKNOWN = "unknown"


@dataclass
class ContactEndpoint:
    """One way to reach the principal.

    ``verified_at`` is the whole point. It is set only by a completed round trip
    (see ``verify.py``), never by configuration, and routing treats ``None`` as
    disqualifying. ``health`` is a softer signal: a degraded endpoint is one that
    *has* worked and is currently unhappy, which is still worth trying before
    giving up entirely.
    """

    kind: str
    address: str
    verified_at: str | None = None
    health: EndpointHealth = EndpointHealth.UNKNOWN
    health_reason: str | None = None
    last_receipt_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ENDPOINT_KINDS:
            raise ValueError(
                f"unknown endpoint kind {self.kind!r}; expected one of {ENDPOINT_KINDS}"
            )

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    def mark_verified(self, receipt_id: str, *, when: datetime | None = None) -> None:
        self.verified_at = (when or datetime.now(UTC)).isoformat()
        self.health = EndpointHealth.OK
        self.health_reason = None
        self.last_receipt_id = receipt_id

    def mark_unhealthy(self, reason: str) -> None:
        """Record a failure without erasing proof that the channel once worked.

        A transient failure must not un-verify a channel — that would let a blip
        permanently remove someone's only route. Previously-verified endpoints
        degrade; never-verified ones fail.
        """
        self.health = EndpointHealth.DEGRADED if self.is_verified else EndpointHealth.FAILED
        self.health_reason = reason


@dataclass
class ChannelPreference:
    """Topic class → ordered endpoint kinds, with an urgency floor.

    ``urgency_floor`` is the threshold at which a message is important enough to
    break quiet hours. Anything below it defers.
    """

    topic_class: str
    ranked_kinds: list[str] = field(default_factory=list)
    urgency_floor: int = 5


@dataclass
class PrincipalProfile:
    """The human behind a handle: their endpoints, and how they want to be reached.

    ``handle`` is the join key and uses the platform grammar (ADR-020), validated
    through :func:`axiom.vega.identity.parse_handle` so this layer cannot drift
    from the identity layer. ``directory_ref`` carries the ADR-103 directory
    object id when one is known — an attribute, deliberately not the key, because
    not every principal comes from a directory and the handle is what the rest of
    the platform already speaks.
    """

    handle: str
    display_name: str
    kind: PrincipalKind = PrincipalKind.HUMAN
    endpoints: list[ContactEndpoint] = field(default_factory=list)
    preferences: list[ChannelPreference] = field(default_factory=list)
    quiet_hours: tuple[str, str] | None = None
    status: PrincipalStatus = PrincipalStatus.PENDING
    directory_ref: str | None = None

    def __post_init__(self) -> None:
        # Reuse the platform's grammar rather than carrying a copy of it.
        self._name, self._context = parse_handle(self.handle)

    @property
    def requires_interview(self) -> bool:
        """Only a person can be interviewed about how they prefer to be reached.

        Asking a mailbox its greeting habits is not a degraded interview, it is
        the wrong operation. Non-human principals are declared (``provision.py``).
        """
        return self.kind.is_human

    @property
    def supports_voice(self) -> bool:
        """Voice is a property of a person.

        A service identity has a *service persona*: declared, not learned, with
        no consent story and no edit-diff loop, because nobody is editing its
        drafts. Keeping this a property rather than an ``if kind == HUMAN`` at
        each call site is what stops the human assumptions bleeding across.
        """
        return self.kind.is_human

    @property
    def name(self) -> str:
        return self._name

    @property
    def context(self) -> str | None:
        return self._context

    def endpoint(self, kind: str) -> ContactEndpoint | None:
        return next((e for e in self.endpoints if e.kind == kind), None)

    def preference_for(self, topic: str) -> ChannelPreference | None:
        """Exact topic match, else the wildcard, else nothing."""
        exact = next((p for p in self.preferences if p.topic_class == topic), None)
        return exact or next((p for p in self.preferences if p.topic_class == "*"), None)

    def refresh_status(self) -> PrincipalStatus:
        """A principal is active only once something has actually round-tripped."""
        if self.status is PrincipalStatus.REVOKED:
            return self.status
        if any(e.is_verified for e in self.endpoints):
            self.status = PrincipalStatus.ACTIVE
        return self.status
