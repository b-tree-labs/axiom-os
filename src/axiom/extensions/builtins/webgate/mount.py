# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MountSpec factory — how ``webgate`` attaches to the composed HTTP app.

Public mount (``requires_authz=False``): the gate is what performs authentication,
so its login + verify routes must be reachable without a prior session. See ADR-003.
"""

from __future__ import annotations

import os

from axiom.extensions.builtins.http.registry import MountSpec
from axiom.infra.web_brand import resolve_web_brand

from .api.routers import LoginBrand, build_webgate_router
from .skills._accounts import ACCOUNTS_ENV

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
            brand=LoginBrand(product_name=brand.product_name, accent=brand.accent),
            user_store=_accounts_store(),
        ),
        extension="webgate",
        requires_authz=False,
        profiles=("server",),
    )


def _accounts_store():
    """The accounts `axi gate adduser` writes, when the node names that file.

    Without this the mounted gate fell back to the process-wide store, which
    starts empty and nothing in production fills, so an account the CLI had
    just reported adding could not sign in. The file store re-reads on change,
    so an account added while the node runs works without a restart, and a
    missing or broken file denies everyone rather than letting anyone in.
    ``None`` (no file named) keeps the process-wide store, so a consumer that
    installs its own with ``set_user_store`` is unaffected.
    """
    path = os.environ.get(ACCOUNTS_ENV, "").strip()
    if not path:
        return None
    from axiom.webauth import JsonFileUserStore

    return JsonFileUserStore(path)
