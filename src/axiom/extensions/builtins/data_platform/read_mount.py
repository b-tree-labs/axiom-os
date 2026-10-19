# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""How the data read attaches to the composed HTTP app."""

from __future__ import annotations

from axiom.extensions.builtins.http.registry import MountSpec

from .read_api import build_data_read_router

PREFIX = "/data"


def mount_spec() -> MountSpec:
    return MountSpec(
        prefix=PREFIX,
        router=build_data_read_router(),
        extension="data_platform",
        requires_authz=True,
        functions=("data_platform",),
        profiles=("server",),
    )


__all__ = ["PREFIX", "mount_spec"]
