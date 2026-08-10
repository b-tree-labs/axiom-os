# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MountSpec factory — how ``webgate`` attaches to the composed HTTP app.

Public mount (``requires_authz=False``): the gate is what performs authentication,
so its login + verify routes must be reachable without a prior session. See ADR-003.
"""

from __future__ import annotations

from axiom.extensions.builtins.http.registry import MountSpec

from .api.routers import build_webgate_router

GATE_PREFIX = "/gate"


def mount_spec() -> MountSpec:
    """Return the public ``/gate`` forward-auth mount."""
    return MountSpec(
        prefix=GATE_PREFIX,
        router=build_webgate_router(),
        extension="webgate",
        requires_authz=False,
        profiles=("server",),
    )
