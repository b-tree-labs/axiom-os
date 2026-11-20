# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Redaction before the wire (unit J3, LB-J3-A13): a credential-shaped string never leaves.

A row's summary is the journal's own words about a record, so it is redacted when it is written
and again on every way out (a row written before this rule existed). The mark is
``[redacted:<pattern>]``, the platform's secret patterns' names, which a surface renders verbatim.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.secret_scan import SECRET_PATTERNS

#: The fields a row carries as free text; ``ref`` holds ids and digests a verify compares.
TEXT_FIELDS = ("summary", "actor", "link")


def redact_text(text: str | None) -> str | None:
    if not text:
        return text
    for name, pattern in SECRET_PATTERNS:
        text = pattern.sub(f"[redacted:{name}]", text)
    return text


def shown(row: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``row`` as a surface may show it; the input is not changed."""
    out = dict(row)
    for field in TEXT_FIELDS:
        value = out.get(field)
        if isinstance(value, str):
            out[field] = redact_text(value)
    return out


__all__ = ["TEXT_FIELDS", "redact_text", "shown"]
