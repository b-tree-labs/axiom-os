# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""One record, one signal — the simplest normalizer that is still correct.

Copy this and change three things: the ``schema_ref`` you register under, where
the value lives in the payload, and the channel name you give it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: The schema_ref this normalizer claims. Registration keys on it exactly.
SCHEMA_REF = "example.single/v1"


def one_reading(record: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Turn one bronze row into one canonical signal.

    The payload is under ``record["row"]``; everything beside it is envelope the
    edge already attached.

    Note what is *not* set here. ``site``, ``schema_ref`` and ``row_hash`` are
    filled in by ``conform_rows`` from the connector mapping and the record
    itself, so setting them here would either duplicate or, worse, disagree.
    """
    payload = record["row"]

    # Raise rather than emit a row you do not believe. A normalizer that raises
    # is counted in the funnel's ``errored`` and the walk continues; a
    # normalizer that emits a null value to avoid raising puts a hole in silver
    # that nothing downstream can distinguish from a real reading.
    value = payload["value"]

    yield {
        "stream": "example",
        "channel": payload["channel"],
        "ts": payload["ts"],
        "value": float(value),
        "unit": payload.get("unit", ""),
        # 'measured' is the default and is stated anyway, because the moment a
        # second normalizer in the same package emits 'predicted' the contrast
        # is what a reader needs to see.
        "source_class": "measured",
    }
