# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The library's mount: ``/api/v1/library``, nested in the web app's ``/api/v1``.

Nested rather than contributed onto the web app's router for one reason: the
web app's mount opts out of the substrate's authz hook and gates per route,
which a gate key from a bound laptop does not satisfy. Claiming the prefix as
its own mount (``nests_in``) puts every library request through the hook, so a
browser session and a gate key reach the same decision, and the handlers read a
principal and a site that were checked (the document-search mount does the same).

``reads`` declares the POST that only reads (classification preview), so a
read-only key may ask it; uploads, changes and deletes stay ``invoke``.
"""

from __future__ import annotations

from axiom.extensions.builtins.http.registry import MountSpec

PREFIX = "/api/v1/library"


def mount_spec() -> MountSpec:
    from axiom.extensions.builtins.library.api import CLASSIFY_PATH, build_router

    return MountSpec(
        prefix=PREFIX,
        router=build_router(),
        extension="library",
        requires_authz=True,
        functions=("rag",),
        reads=(PREFIX + CLASSIFY_PATH,),
        nests_in=("/api/v1",),
    )


__all__ = ["PREFIX", "mount_spec"]
