# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Advisory batch check. Neutral, and never a required check.

Posting the check to a forge is disarmed. ``advisory_status`` is the
payload a later release may post; ``post_status`` on a forge raises.
"""

from __future__ import annotations

from axiom.extensions.builtins.release.lander.loop_guard import refuse_remote_write


def advisory_status(batch_key: str, members: list[int]) -> dict[str, object]:
    return {
        "context": "rivet/batch",
        "conclusion": "neutral",
        "blocking": False,
        "description": (
            f"batch {batch_key}: {len(members)} pull requests "
            "(advisory, not a required check)"
        ),
    }


def assert_advisory(payload: dict[str, object]) -> None:
    """A warn-mode check is neutral and non-blocking."""
    if payload.get("conclusion") != "neutral" or payload.get("blocking") is not False:
        raise ValueError("a batch check in warn mode must be neutral and non-blocking")


def refuse_status_post(payload: dict[str, object]) -> None:
    """Validate the payload, then refuse to post it."""
    assert_advisory(payload)
    refuse_remote_write("status_post")
