# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The library's slice of ``/api/v1``.

Registered through the webapp extension's contribution registry rather than
mounted on its own prefix, so a client talks to one surface with one auth
posture. This module is the entry the manifest names; webapp resolves it lazily
and never imports this package at load time.
"""

from __future__ import annotations

from typing import Any


def register_routes(router: Any, *, subpath: str = "/library") -> None:
    """Attach the library endpoints under ``/api/v1<subpath>``."""
    from fastapi import HTTPException

    from axiom.extensions.builtins.library.taxonomy import (
        TaxonomyError,
        get_taxonomy,
        registered_taxonomies,
    )

    @router.get(subpath + "/taxonomies", tags=["library"])
    def taxonomies() -> dict:
        """Which document vocabularies this installation has.

        A product registers its own, so the answer is a property of the
        deployment rather than of the platform.
        """
        return {"taxonomies": list(registered_taxonomies())}

    @router.get(subpath + "/taxonomies/{name}", tags=["library"])
    def taxonomy(name: str) -> dict:
        """One vocabulary: its categories in order, and the types that file there."""
        try:
            found = get_taxonomy(name)
        except TaxonomyError as exc:
            # The message names what *is* registered, which turns a 404 into a
            # usable answer rather than a dead end.
            raise HTTPException(404, str(exc)) from exc

        return {
            "name": found.name,
            "category_order": list(found.category_order),
            "fallback_category": found.fallback_category,
            "types": [
                {"key": t.key, "label": t.label, "category": t.category}
                for t in found.types
            ],
        }


__all__ = ["register_routes"]
