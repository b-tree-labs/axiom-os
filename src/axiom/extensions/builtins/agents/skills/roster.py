# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who can be asked."""

from __future__ import annotations

from typing import Any


def roster(**_: Any) -> dict:
    """List the agents ``agents.ask`` can address.

    A delegation surface whose roster cannot be listed forces a caller to guess
    names, which is how `did_you_mean` became the most exercised path in the
    previous tool.
    """
    from axiom.extensions.builtins.connect.agent_router import discover_agents

    return {"agents": sorted(discover_agents())}


__all__ = ["roster"]
