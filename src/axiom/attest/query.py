# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Read signed records of one chain, filtered (F1 design note, question 5).

Filters run over ``service.records`` in Python; there is no SQL of its own. Records come back
as stored, in ``seq`` order, so they can be handed to ``verify_chain``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from axiom.extensions.builtins.attest import service

from ._site import site_or_refuse

__all__ = ["records"]


def _in_interval(record: dict[str, Any], interval: tuple[str, Any]) -> bool:
    kind, number = interval
    got = record.get("interval") or {}
    return got.get("kind") == kind and str(got.get("number")) == str(number)


def _has_text(record: dict[str, Any], text: str) -> bool:
    # The rule search/engine.py:logbooks uses: a case-insensitive substring of the content.
    content = json.dumps(record.get("content") or {}, ensure_ascii=False, default=str)
    return text.lower() in content.lower()


def records(
    logbook: str,
    *,
    site: str | None = None,
    interval: tuple[str, Any] | None = None,
    meanings: Iterable[str] | None = None,
    text: str | None = None,
    from_seq: int = 1,
) -> list[dict[str, Any]]:
    out = service.records(site_or_refuse(site), logbook, from_seq=from_seq)
    if interval is not None:
        out = [r for r in out if _in_interval(r, interval)]
    if meanings is not None:
        wanted = set(meanings)
        out = [r for r in out if r.get("meaning") in wanted]
    if text:
        out = [r for r in out if _has_text(r, text)]
    return out
