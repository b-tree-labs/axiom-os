# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A minimal mount entry for testing manifest-driven composition — stands in
for an installed package's extension (e.g. appkit on a bare node)."""

from __future__ import annotations


def mount_spec():
    from fastapi import APIRouter

    from axiom.extensions.builtins.http.registry import MountSpec

    router = APIRouter()

    @router.get("/_demo/health")
    def health() -> dict:
        return {"demo": "ok"}

    return MountSpec(
        prefix="/_demo",
        router=router,
        extension="demo",
        requires_authz=False,
        profiles=("server",),
    )
