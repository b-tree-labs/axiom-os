# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Per-source extraction (ADR-087 D1): the fragments that entered from
one source coordinate, alias sets included.

The origin coordinate survives re-homing and merging, so extraction is
two-legged:

1. live fragments whose ``provenance.origin`` matches the source;
2. canonical fragments that a merge folded a matching source coordinate
   into (``memory_alias`` records — D3's no-silent-loss guarantee).
"""

from __future__ import annotations

from typing import Any

from axiom.memory.fragment import MemoryFragment, fragment_from_dict


def extract_by_source(
    composition: Any,
    *,
    harness: str,
    account: str | None = None,
    principal: str | None = None,
) -> list[MemoryFragment]:
    """Exactly the live fragments that entered from ``(harness[, account])``.

    Merged-away witnesses resolve to their canonical fragment via the
    alias set; each fragment appears once. Order: registry order
    (created_at ascending), alias-resolved canonicals appended.
    """
    from axiom.memory.dedup import aliases_for_source

    results: list[MemoryFragment] = []
    seen: set[str] = set()

    for artifact in composition.artifact_registry.list(kind="fragment"):
        data = artifact.data or {}
        prov = data.get("provenance") or {}
        origin = prov.get("origin")
        if not origin:
            continue
        if origin.get("harness") != harness:
            continue
        if account is not None and origin.get("account") != account:
            continue
        if principal is not None and prov.get("principal_id") != principal:
            continue
        if data.get("id") in seen:
            continue
        seen.add(data["id"])
        results.append(fragment_from_dict(data))

    for alias in aliases_for_source(
        composition, harness=harness, account=account
    ):
        if principal is not None and alias.get("principal") != principal:
            continue
        canonical_id = alias.get("canonical_id")
        if not canonical_id or canonical_id in seen:
            continue
        artifacts = composition.artifact_registry.find_by_name(
            "fragment", canonical_id
        )
        if not artifacts:
            continue
        seen.add(canonical_id)
        results.append(fragment_from_dict(artifacts[0].data))

    return results


__all__ = ["extract_by_source"]
