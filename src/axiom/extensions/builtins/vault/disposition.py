# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What can actually be DONE about a credential — and the question that follows.

The expiry audit asked one question of everything: when does this expire. Six
credentials had no answer, so they were reported red every run with the same
advice, "record one", which is wrong for most of them. A PyPI token has no
expiry to record. A service account on somebody else's network is not ours to
rotate at all. An Entra secret does expire, and only a person with console
access can learn the date.

An unclearable finding is worse than no finding. It repeats until people filter
the report, and then the one real warning lands in a channel nobody reads —
which is how a dead credential survived six months here.

So a credential declares its **disposition**, and the audit asks the question
that disposition makes answerable:

``self_rotatable``
    We hold the authority and an API exists. Rotation falls due at a FRACTION of
    the lifetime, never at expiry: a self-rotating token authenticates as
    itself, so once expired it can no longer rotate. Expiry as the trigger is
    the wall this episode hit.

``human_rotatable``
    It expires, but only a person with console access can read or change the
    date. Automation cannot help; the remedy points at where to look.

``externally_owned``
    Somebody else controls it. Rotating it is not ours to do and suggesting so
    invites breaking a system we do not run. It needs a named owner and a review
    cadence.

``non_expiring``
    There is no expiry to record. Writing one would be a lie that fires a false
    alarm on an arbitrary day.

For everything but the first, **proof of life replaces expiry**. We cannot
predict death for a credential we do not control; we can notice it within one
heartbeat instead of six months. ``last_verified_at`` is that heartbeat, and it
going stale is its own finding — a future review date says when to look again,
not that the credential still works.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: The declared vocabulary. A value outside it is refused rather than treated as
#: undeclared: a typo that silently reverts would let somebody believe they had
#: classified a credential when they had not.
DISPOSITIONS: tuple[str, ...] = (
    "self_rotatable",
    "human_rotatable",
    "externally_owned",
    "non_expiring",
)

#: Rotate a self-rotatable credential this far into its lifetime. Two thirds
#: leaves a third of the window for the rotation to fail, be noticed, and be
#: retried — while the credential still works well enough to rotate itself.
ROTATE_AT_FRACTION = 2.0 / 3.0

#: Proof of life older than this is stale. A credential nothing has exercised in
#: three months may already be dead and nobody would know.
VERIFICATION_STALE_DAYS = 90


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def rotation_due_at(*, issued_at: datetime, expires_at: datetime) -> datetime:
    """When a self-rotatable credential should be rotated.

    A fraction of the way through its life, never at the end. A self-rotating
    credential that reaches its expiry can no longer authenticate to rotate
    itself, so waiting until expiry guarantees a human has to intervene — which
    is exactly what happened.
    """
    return issued_at + (expires_at - issued_at) * ROTATE_AT_FRACTION


def _finding(status: str, remedy: str, *, rotatable: bool, **extra: Any) -> dict[str, Any]:
    return {"status": status, "remedy": remedy, "rotatable": rotatable, **extra}


def classify(meta: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """What this credential needs, given what can be done about it."""
    now = now or datetime.now(UTC)
    disposition = str(meta.get("disposition") or "").strip()
    if disposition and disposition not in DISPOSITIONS:
        raise ValueError(
            f"unknown disposition {disposition!r}; expected one of "
            f"{', '.join(DISPOSITIONS)}"
        )

    name = meta.get("name", "a credential")
    expires = _parse(meta.get("expires_at"))
    verified = _parse(meta.get("last_verified_at"))
    verified_days = (now - verified).days if verified else None

    def _verification_check() -> dict[str, Any] | None:
        """Stale proof of life is a finding on its own.

        A future review date says when to look again. It does not say the
        credential still works, and those are different claims.
        """
        if verified_days is not None and verified_days > VERIFICATION_STALE_DAYS:
            return _finding(
                "verification_stale",
                f"nothing has proved {name} works in {verified_days} days; "
                f"run `axi vault reconcile` or exercise it",
                rotatable=disposition == "self_rotatable",
                verified_days_ago=verified_days,
            )
        return None

    if disposition == "self_rotatable":
        issued = _parse(meta.get("last_rotated_at")) or _parse(meta.get("created_at"))
        if expires and issued:
            due = rotation_due_at(issued_at=issued, expires_at=expires)
            if now >= due:
                return _finding(
                    "rotation_due",
                    f"rotate {name} now — it expires {expires.date()} and a "
                    f"self-rotating credential cannot rotate itself once expired",
                    rotatable=True,
                    rotation_due_at=due.date().isoformat(),
                    verified_days_ago=verified_days,
                )
        stale = _verification_check()
        if stale:
            return stale
        if not expires:
            return _finding(
                "needs_expiry_from_issuer",
                f"{name} is self-rotatable but has no expiry recorded; "
                f"`axi vault reconcile --apply` can fetch it from the issuer",
                rotatable=True,
                verified_days_ago=verified_days,
            )
        return _finding("ok", "", rotatable=True, verified_days_ago=verified_days)

    # Everything below this line is something we cannot rotate on our own.
    if expires:
        stale = _verification_check()
        return stale or _finding("ok", "", rotatable=False,
                                 verified_days_ago=verified_days)

    if not disposition:
        return _finding(
            "undeclared",
            f"{name} has no expiry and no disposition. Declare one so the right "
            f"question can be asked: "
            + ", ".join(DISPOSITIONS)
            + ". 'Record an expiry' is the wrong advice for most of these.",
            rotatable=False,
            verified_days_ago=verified_days,
        )

    if disposition == "human_rotatable":
        return _finding(
            "needs_expiry_from_console",
            f"{name} expires but cannot be queried — read the date from the "
            f"provider's console and record it with "
            f"`axi secrets rotate {name} --expires-at <YYYY-MM-DD>`",
            rotatable=False,
            verified_days_ago=verified_days,
        )

    if disposition == "externally_owned" and not str(meta.get("owner") or "").strip():
        return _finding(
            "needs_owner",
            f"{name} is controlled by somebody else — record the owner to "
            f"contact, or the finding is unactionable",
            rotatable=False,
            verified_days_ago=verified_days,
        )

    review = _parse(meta.get("review_by"))
    if not review:
        return _finding(
            "needs_review_by",
            f"{name} has no expiry to record — set a review_by date instead, so "
            f"something falls due even though nothing expires",
            rotatable=False,
            verified_days_ago=verified_days,
        )
    if now >= review:
        return _finding(
            "review_overdue",
            f"{name} was due for review on {review.date()}",
            rotatable=False,
            verified_days_ago=verified_days,
        )

    stale = _verification_check()
    return stale or _finding("ok", "", rotatable=False, verified_days_ago=verified_days)


__all__ = [
    "DISPOSITIONS",
    "ROTATE_AT_FRACTION",
    "VERIFICATION_STALE_DAYS",
    "classify",
    "rotation_due_at",
]
