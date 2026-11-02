# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Hard refusals. Split out so config and the loop share them without a cycle."""

from __future__ import annotations

_REFUSED = frozenset({"merge", "enqueue", "force_push", "push", "status_post"})


class LanderRefused(RuntimeError):
    """The lander was asked to do something this release will not do."""


def refuse_remote_write(action: str) -> None:
    """Merge, enqueue, force-push, and status posts are refused."""
    if action in _REFUSED:
        raise LanderRefused(f"{action} is not enabled; the lander only proposes and reports")
