# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``HttpTabularSource`` — tabular rows from a CSV/JSON endpoint over HTTP(S).

The row-lane counterpart to a document source: :meth:`fetch_rows` GETs the
endpoint and parses it into a :class:`RowBatch`. Change-detection is content-
based — the endpoint is one logical batch (``"current"``), so the bronze row
``content_hash`` dedup tier decides what is actually new run to run.

Stdlib only (``urllib`` + ``csv`` + ``json``) — no new dependency for the most
common structured-feed shape.
"""

from __future__ import annotations

import csv
import io
import json as _json
import urllib.request
from datetime import datetime

from ...contracts import RowBatch


def _http_get(url: str, *, timeout: int = 30, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — operator-supplied URL
        return resp.read(), {k: v for k, v in resp.headers.items()}


def parse_rows(raw: bytes, fmt: str) -> list[dict]:
    """Parse a fetched payload into rows. ``csv`` (with BOM tolerance) or
    ``json`` (an array of objects, a ``{"rows"|"data"|"results"|"items": [...]}``
    envelope, or a single object)."""
    if fmt == "json":
        data = _json.loads(raw.decode("utf-8"))
        if isinstance(data, list):
            return [dict(r) for r in data]
        if isinstance(data, dict):
            for key in ("rows", "data", "results", "items"):
                if isinstance(data.get(key), list):
                    return [dict(r) for r in data[key]]
            return [dict(data)]
        raise ValueError("unsupported JSON shape for http-tabular (want array or object)")
    text = raw.decode("utf-8-sig")  # tolerate a BOM
    reader = csv.DictReader(io.StringIO(text))
    return [dict(r) for r in reader if any((v or "").strip() for v in r.values())]


class HttpTabularSource:
    """A pollable tabular source backed by an HTTP(S) endpoint."""

    def __init__(
        self,
        *,
        name: str,
        url: str,
        fmt: str,
        schema_ref: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.url = url
        self.fmt = fmt
        self.schema_ref = schema_ref
        self._headers = headers or {}

    def list_changed(self, since: datetime | None = None) -> list[str]:
        # One endpoint = one logical batch; content_hash dedup handles "unchanged".
        return ["current"]

    def fetch_rows(self, item: str) -> RowBatch:
        raw, hdrs = _http_get(self.url, headers=self._headers)
        rows = parse_rows(raw, self.fmt)
        etag = hdrs.get("ETag") or hdrs.get("etag")
        return RowBatch(
            source_name=self.name,
            item_id=item,
            etag=etag,
            modified_at=None,
            schema_ref=self.schema_ref,
            rows=rows,
            raw=raw,
            source_path=self.url,
        )


__all__ = ["HttpTabularSource", "parse_rows"]
