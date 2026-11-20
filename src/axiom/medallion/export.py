# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Medallion export primitives — pseudonymization + consent filtering.

Domain-agnostic building blocks for research-data export. Extensions
compose these into domain-specific exporters (e.g. classroom's
trace_export.py assembles trace + quiz + interview rows using these).

Design principles:
- Deterministic: same input → same pseudonym across runs (longitudinal
  research requires stable pseudonyms).
- Consent-aware: explicit allowlist set; non-consenting principals
  excluded before any write.
- Stateless: pure functions; no I/O beyond the caller's write target.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Pseudonymization
# ---------------------------------------------------------------------------


def pseudonymize(identifier: str, length: int = 10) -> str:
    """Deterministic pseudonym via sha256 truncation.

    Same input → same pseudonym across runs and across bundle artifacts,
    which lets researchers join trace + quiz + interview rows for the
    same principal without revealing the real identifier.
    """
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    return f"anon-{digest[:length]}"


def maybe_pseudonymize(identifier: str, anonymize: bool) -> str:
    """Helper: apply pseudonymization conditionally."""
    if not identifier:
        return identifier
    return pseudonymize(identifier) if anonymize else identifier


# ---------------------------------------------------------------------------
# Consent filter
# ---------------------------------------------------------------------------


def consent_filter(
    rows: Iterable[dict],
    consented_ids: set[str] | None,
    id_key: str = "principal_id",
) -> list[dict]:
    """Return only rows whose `id_key` value is in `consented_ids`.

    If `consented_ids` is None, the filter is inactive (all rows pass).
    Applies BEFORE pseudonymization so consent checks run against real
    identifiers.
    """
    if consented_ids is None:
        return list(rows)
    return [r for r in rows if r.get(id_key) in consented_ids]
