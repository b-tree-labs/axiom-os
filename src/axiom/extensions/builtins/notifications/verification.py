# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Evidence that an effect happened, rather than that a call returned.

Every value in the delivery-outcome vocabulary — pending, succeeded, failed,
denied, expired — describes what happened to the *call*. None describes whether
the *effect* is observable. A week of defects lived in that gap: a receipt
issued for an alert written to a store discarded at process exit; a release tool
exiting 0 having published nothing; a workflow reporting failure for a release
that had shipped; a chat narrating tool calls it never made. None were caught by
their own reporting — each surfaced when somebody checked the underlying thing
by hand.

So verification here means going and looking. ``verify_readback`` fetches the
written row back through the same path a reader would use. That is a weaker
guarantee than a human acknowledging an alert and a much stronger one than a
function having returned.

Three states, kept distinct because collapsing them is how the original bug
reads as success:

- **verified** — it was looked for and found, in a store that outlives this
  process.
- **checked but not verified** — it was looked for and was not there. This is
  evidence of absence.
- **not checked** — the look itself failed. "I could not tell" is not the same
  claim as "it did not happen", and a caller that treats them alike will either
  cry wolf or hide a real loss.

Nothing here raises. Verification runs inside a delivery path, and a delivery
that fails because its *audit* failed would be a worse bug than the one this
exists to catch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerificationResult:
    """What was established about an effect, and how."""

    verified: bool
    checked: bool
    method: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "checked": self.checked,
            "method": self.method,
            "detail": self.detail,
        }


def verify_readback(store: Any, *, row_id: str, recipient: str) -> VerificationResult:
    """Confirm a written alert can be read back out of ``store``.

    A read-back through the reader's own path is the cheapest honest evidence
    available at write time: it proves the row is where a reader will look,
    rather than that a write call returned.
    """
    if not getattr(store, "durable", False):
        # Reading back from an in-process store proves only that this process
        # can see it — which is exactly what was previously mistaken for
        # delivery. Not a failure; simply not evidence.
        return VerificationResult(
            verified=False,
            checked=False,
            method="readback",
            detail=(
                "store does not outlive this process, so a read-back proves "
                "nothing about what anyone else can see"
            ),
        )

    try:
        from axiom.extensions.builtins.notifications.inbox import InboxQuery

        rows = store.query(InboxQuery(recipient=recipient))
    except Exception as exc:  # noqa: BLE001 - auditing must not break delivery
        _log.debug("delivery verification could not run", exc_info=True)
        return VerificationResult(
            verified=False,
            checked=False,
            method="readback",
            detail=f"could not read back: {type(exc).__name__}: {exc}",
        )

    found = any(getattr(r, "id", None) == row_id for r in rows or ())
    if found:
        return VerificationResult(
            verified=True, checked=True, method="readback",
            detail=f"row {row_id} readable by {recipient}",
        )
    return VerificationResult(
        verified=False, checked=True, method="readback",
        detail=(
            f"row {row_id} was written but not found on read-back for "
            f"{recipient}; it will not surface in their inbox"
        ),
    )


__all__ = ["VerificationResult", "verify_readback"]
