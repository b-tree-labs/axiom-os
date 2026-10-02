# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MountSpec factory — how ``webgate`` attaches to the composed HTTP app.

Public mount (``requires_authz=False``): the gate is what performs authentication,
so its login + verify routes must be reachable without a prior session. See ADR-003.
"""

from __future__ import annotations

from axiom.extensions.builtins.http.registry import MountSpec
from axiom.infra.web_brand import resolve_web_brand

from .api.routers import LoginBrand, build_webgate_router

GATE_PREFIX = "/gate"


def mount_spec() -> MountSpec:
    """Return the public ``/gate`` forward-auth mount.

    The brand is resolved HERE, per call, rather than left to
    ``LoginBrand``'s defaults. The gate is the first page an unauthenticated
    person reaches, so a node that names itself in the environment must be
    able to say that name before anyone has signed in; leaving the default
    literal to win meant the sign-in card introduced the platform instead of
    the product on every branded deploy.

    Only the identity fields travel. ``footer`` keeps "Protected by Axiom" —
    per ADR-048 branding white-labels who you are signing in TO, not the
    platform doing the protecting.
    """
    brand = resolve_web_brand()
    return MountSpec(
        prefix=GATE_PREFIX,
        router=build_webgate_router(
            brand=LoginBrand(product_name=brand.product_name, accent=brand.accent)
        ),
        extension="webgate",
        requires_authz=False,
        profiles=("server",),
    )
