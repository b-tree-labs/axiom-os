# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``PushSource`` — the runtime object behind a push-only connector.

A push connector is written by producers that POST to the ingest face
(``/ingest``, ``/ingest/rows``); the platform never pulls from it. The source
therefore lists nothing changed and cannot fetch: a scheduled pull tick over
it is a no-op, never an error, and the connector's config (bronze root, rules,
disposition, tier) is what the face's sink resolvers build the writer from.
"""

from __future__ import annotations

from datetime import datetime


class PushSource:
    """Both shapes at once (document + tabular): nothing to list, nothing to fetch."""

    def __init__(self, name: str, *, schema_ref: str = "") -> None:
        self.name = name
        self.schema_ref = schema_ref

    def list_changed(self, since: datetime | None = None) -> list[str]:
        return []

    def fetch(self, item: str):
        raise KeyError(f"push connector {self.name!r} has nothing to fetch: {item!r}")

    def fetch_rows(self, item: str):
        raise KeyError(f"push connector {self.name!r} has nothing to fetch: {item!r}")


__all__ = ["PushSource"]
