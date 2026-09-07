# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Serving boundary — one door out, fail-closed (ADR-087 D7).

Symmetric with the write invariant (one door in via ``CompositionService``),
every path that serves a user's memory back into a harness passes ONE gate,
after retrieval and before text serialization. The gate is never delegated to
an adapter or the caller — the three transports (MCP tool, plain-text block,
query endpoint) all funnel through :class:`ServingGate`.

Doubt → deny (security doc §2), enumerated on :class:`DenyReason`:

- ``VAULT`` — ``vault`` never serves, **unconditional**: no configuration, no
  trusted-consumer exemption, evaluated before anything else.
- ``SECRET_ROUTED_TO_VAULT`` — content that reads as a programmatic secret is
  routed to vault (and thereby unservable). This is the outbound half of the
  same classifier the absorb side uses to keep secrets out of plain fragments
  (P2 OQ6 resolution; security doc §4).
- ``UNLABELED`` — a missing or unknown ``visibility`` / ``classification``
  label. Unlabeled ≠ public.
- ``POLICY_ERROR`` / ``POLICY_UNAVAILABLE`` — the policy engine raised. A
  raising policy source is *unavailable*, never *healthy*.
- ``POLICY_DENIED`` — the policy engine cleanly said no.
- ``UNRESOLVED_CONSUMER`` — the consumer's identity/entitlement (principal +
  account) could not be resolved.
- ``CROSS_ACCOUNT`` — the fragment's storage account is not compatible with the
  consumer's. Work and personal memory never blend in either direction.
- ``TIER_MISMATCH`` — content restricted to a local/controlled tier would ride
  a prompt to a remote endpoint. A locally hosted model and a remote
  third-party API are different exposure domains.

Corollary — **no-push rule** (D7 / security doc §3): fragments are never pushed
into a foreign retrieval store. Serving stays query-time so the gate runs per
request. :func:`refuse_push` is the explicit, always-raising guard the fusion
path calls to make that property structural, not merely documented.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from axiom.vega.federation.policy import (
    ClassificationStamp,
    VisibilityHorizon,
)

if TYPE_CHECKING:
    from axiom.memory.fragment import MemoryFragment

# Deployment tiers, ordered controlled → exposed. A fragment restricted to the
# controlled tier must never ride a prompt to an exposed (remote) endpoint.
TIER_LOCAL = "local"
TIER_REMOTE = "remote"
_TIER_EXPOSURE: dict[str, int] = {TIER_LOCAL: 0, TIER_REMOTE: 1}

_KNOWN_VISIBILITIES: frozenset[str] = frozenset(v.value for v in VisibilityHorizon)
_KNOWN_CLASSIFICATION_LEVELS: frozenset[str] = frozenset(
    {"unclassified", "cui", "secret", "top_secret"}
)


# ---------------------------------------------------------------------------
# No-push rule (D7 corollary)
# ---------------------------------------------------------------------------


class NoPushError(RuntimeError):
    """Raised on any attempt to push memory into a foreign retrieval store.

    Pushed content escapes per-request policy evaluation, survives entitlement
    changes, and is unreachable by ``forget()``. The only sanctioned exit is
    the explicit export ceremony (ADR-087 D9).
    """


def refuse_push(target: str = "") -> None:
    """Always raise :class:`NoPushError` — serving is query-time only."""
    raise NoPushError(
        f"refusing to push memory into foreign store {target!r}: the no-push "
        "rule keeps serving query-time so the gate runs per request "
        "(ADR-087 D7). Use `axi memory export` for the sanctioned exit."
    )


# ---------------------------------------------------------------------------
# Secret detection (OQ6) — outbound half of the absorb-side classifier
# ---------------------------------------------------------------------------

# Conservative, high-precision patterns. The failure-mode asymmetry (a false
# negative costs one query some recall; a false positive is permanent
# exfiltration) argues for catching obvious programmatic secrets while not
# flagging ordinary prose.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{12,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "labeled_secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b"
            r"\s*[:=]\s*['\"]?[^\s'\"]{8,}"
        ),
    ),
)


def looks_like_secret(text: str) -> str | None:
    """Return the matched pattern name if ``text`` reads as a secret, else None.

    The same classifier gates the absorb side (secrets → vault on the way in)
    and this serving boundary (secret-class content never leaves). Kept
    high-precision on purpose.
    """
    if not text:
        return None
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return name
    return None


# ---------------------------------------------------------------------------
# Deny reasons + decision shapes
# ---------------------------------------------------------------------------


class DenyReason(str, Enum):
    """Enumerated fail-closed denial reasons (security doc §2)."""

    VAULT = "vault"
    SECRET_ROUTED_TO_VAULT = "secret_routed_to_vault"
    UNLABELED = "unlabeled"
    POLICY_ERROR = "policy_error"
    POLICY_UNAVAILABLE = "policy_unavailable"
    POLICY_DENIED = "policy_denied"
    UNRESOLVED_CONSUMER = "unresolved_consumer"
    CROSS_ACCOUNT = "cross_account"
    TIER_MISMATCH = "tier_mismatch"


class PolicyUnavailable(Exception):
    """A policy source that is unreachable (distinct from one that errored).

    Both fail closed; the reason is recorded distinctly so audit can tell an
    outage from a bug.
    """


@dataclass(frozen=True)
class ServingDecision:
    """One fragment's gate outcome."""

    allowed: bool
    reason: DenyReason | None = None
    detail: str = ""


@dataclass(frozen=True)
class Denial:
    """A denied fragment, for audit + transport reporting."""

    fragment_id: str
    reason: DenyReason
    detail: str = ""


# ---------------------------------------------------------------------------
# Consumer coordinate + servable view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsumerCoordinate:
    """Who is asking, and from where (security doc §2).

    Entitlement is evaluated against ``(harness, account, deployment tier)`` —
    the consumer's *whole storage domain*, not the single prompt. ``account``
    is the identity a given store belongs to (work vs personal); the gate
    serves only what is acceptable to persist in the consumer's least-controlled
    store.
    """

    principal: str
    harness: str
    account: str
    deployment_tier: str = TIER_LOCAL
    model_endpoint: str = ""
    compatible_accounts: frozenset[str] = field(default_factory=frozenset)

    @property
    def resolved(self) -> bool:
        """False when the consumer's identity/entitlement is unresolvable."""
        return bool(self.principal) and bool(self.account)

    @property
    def accounts(self) -> frozenset[str]:
        """The account set this consumer may receive (always includes its own)."""
        return frozenset({self.account, *self.compatible_accounts}) - {""}


@dataclass(frozen=True)
class ServableItem:
    """The post-retrieval, pre-serialization view the gate evaluates.

    Carries exactly the labels the ADR-088 ``rag-memory`` chunk contract
    promises (``cognitive_type``, ``visibility``, ``classification``) plus the
    storage account, so the gate never needs to re-open the ledger — and so a
    chunk that is missing a label is a real, testable unlabeled-deny.
    """

    fragment_id: str
    cognitive_type: str | None
    visibility: str | None
    classification: dict | None
    account: str
    text: str

    @classmethod
    def from_fragment(cls, fragment: MemoryFragment) -> ServableItem:
        from axiom.memory.recall_projection import _render_text

        origin = fragment.provenance.origin
        account = origin.account if origin is not None else fragment.provenance.principal_id
        return cls(
            fragment_id=fragment.id,
            cognitive_type=fragment.cognitive_type.value,
            visibility=fragment.visibility.value,
            classification=fragment.classification.to_dict(),
            account=account,
            text=_render_text(fragment.cognitive_type, fragment.content),
        )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

PolicyFn = Callable[["ServableItem", "ConsumerCoordinate"], bool]


@dataclass
class ServingGate:
    """The single fail-closed serving gate (ADR-087 D7).

    ``policy`` is the optional per-item policy engine
    (``(item, consumer) -> bool``). It may raise :class:`PolicyUnavailable`
    (outage) or any other exception (bug) — both deny. A clean ``False`` is a
    legitimate policy denial. ``secret_detector`` defaults to
    :func:`looks_like_secret`.
    """

    policy: PolicyFn | None = None
    secret_detector: Callable[[str], str | None] = looks_like_secret

    def evaluate(
        self, item: ServableItem, consumer: ConsumerCoordinate
    ) -> ServingDecision:
        """Evaluate one fragment. Any unexpected error denies (fail-closed)."""
        try:
            return self._evaluate(item, consumer)
        except PolicyUnavailable as exc:  # pragma: no cover - defensive
            return ServingDecision(False, DenyReason.POLICY_UNAVAILABLE, str(exc))
        except Exception as exc:  # noqa: BLE001 - fail-closed catch-all
            return ServingDecision(False, DenyReason.POLICY_ERROR, str(exc))

    def _evaluate(
        self, item: ServableItem, consumer: ConsumerCoordinate
    ) -> ServingDecision:
        # 0. Consumer identity must resolve.
        if not consumer.resolved:
            return ServingDecision(
                False, DenyReason.UNRESOLVED_CONSUMER,
                "consumer principal/account unresolved",
            )

        # 1. vault — unconditional, before anything else.
        if item.cognitive_type == "vault":
            return ServingDecision(
                False, DenyReason.VAULT, "vault never serves",
            )

        # 2. Secret-class content routes to vault (OQ6) → never serves.
        matched = self.secret_detector(item.text or "")
        if matched:
            return ServingDecision(
                False, DenyReason.SECRET_ROUTED_TO_VAULT,
                f"secret pattern: {matched}",
            )

        # 3. Labels must be present and known (unlabeled ≠ public).
        if item.visibility not in _KNOWN_VISIBILITIES:
            return ServingDecision(
                False, DenyReason.UNLABELED,
                f"unknown visibility: {item.visibility!r}",
            )
        level = (item.classification or {}).get("level")
        if item.classification is None or level not in _KNOWN_CLASSIFICATION_LEVELS:
            return ServingDecision(
                False, DenyReason.UNLABELED,
                f"unknown classification level: {level!r}",
            )

        # 4. Cross-account — work and personal never blend.
        if item.account not in consumer.accounts:
            return ServingDecision(
                False, DenyReason.CROSS_ACCOUNT,
                f"fragment account {item.account!r} not in consumer domain "
                f"{sorted(consumer.accounts)}",
            )

        # 5. Deployment tier — controlled content never rides to remote.
        effective = self._effective_outflow(item)
        if self._is_controlled(effective) and self._is_exposed(consumer.deployment_tier):
            return ServingDecision(
                False, DenyReason.TIER_MISMATCH,
                f"controlled content ({effective.value}) cannot serve to "
                f"{consumer.deployment_tier!r} endpoint",
            )

        # 6. Policy engine — raise denies (fail-closed); clean False denies.
        if self.policy is not None:
            try:
                ok = self.policy(item, consumer)
            except PolicyUnavailable as exc:
                return ServingDecision(
                    False, DenyReason.POLICY_UNAVAILABLE, str(exc),
                )
            except Exception as exc:  # noqa: BLE001 - a raising source is unavailable
                return ServingDecision(False, DenyReason.POLICY_ERROR, str(exc))
            if not ok:
                return ServingDecision(
                    False, DenyReason.POLICY_DENIED, "policy denied",
                )

        return ServingDecision(True)

    def filter(
        self, items: list[ServableItem], consumer: ConsumerCoordinate
    ) -> tuple[list[ServableItem], list[Denial]]:
        """Partition items into (allowed, denied) — the transport-facing call."""
        allowed: list[ServableItem] = []
        denials: list[Denial] = []
        for item in items:
            decision = self.evaluate(item, consumer)
            if decision.allowed:
                allowed.append(item)
            else:
                denials.append(
                    Denial(
                        fragment_id=item.fragment_id,
                        reason=decision.reason or DenyReason.POLICY_ERROR,
                        detail=decision.detail,
                    )
                )
        return allowed, denials

    # ---- tier helpers ------------------------------------------------------

    @staticmethod
    def _effective_outflow(item: ServableItem) -> VisibilityHorizon:
        vis = VisibilityHorizon(item.visibility)  # known by step 3
        stamp = ClassificationStamp.from_dict(item.classification or {})
        return VisibilityHorizon.most_restrictive(vis, stamp.allowed_outflow_level())

    @staticmethod
    def _is_controlled(effective: VisibilityHorizon) -> bool:
        # SCOPE_INTERNAL content is controlled to the local/owned tier.
        return effective is VisibilityHorizon.SCOPE_INTERNAL

    @staticmethod
    def _is_exposed(tier: str) -> bool:
        return _TIER_EXPOSURE.get(tier, 1) >= _TIER_EXPOSURE[TIER_REMOTE]


__all__ = [
    "TIER_LOCAL",
    "TIER_REMOTE",
    "ConsumerCoordinate",
    "Denial",
    "DenyReason",
    "NoPushError",
    "PolicyUnavailable",
    "ServableItem",
    "ServingDecision",
    "ServingGate",
    "looks_like_secret",
    "refuse_push",
]
