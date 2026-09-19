# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""One record, many channels — the fan-out case, and the one that goes wrong.

A frame arrives holding several channels at a single timestamp. Canonical silver
is narrow: one row per channel. So this yields several rows from one record, and
that is where the primary key starts to matter.

``silver.signals`` is keyed ``(row_hash, channel)`` and the upsert is
``ON CONFLICT DO NOTHING``. Every row this yields inherits the *same* row_hash,
because they all came from one bronze record. **So two yielded rows with the
same channel name collide, and the second is silently discarded** — no error, no
warning, and a funnel that still reports both in ``rows_out``.

That is the failure this file exists to demonstrate. It is not hypothetical: any
payload with a repeated key, a channel name derived from a non-unique field, or
a naming collision between two sub-sections produces it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SCHEMA_REF = "example.frame/v1"

#: Payload key -> canonical channel name.
#:
#: An explicit map rather than passing payload keys straight through. Two
#: reasons, and the second is the important one: a renamed field upstream should
#: break loudly here instead of quietly creating a new channel nobody queries,
#: and a mapping is where you notice that two payload keys would produce the
#: same channel name.
CHANNELS = {
    "temp_a": "temperature.a",
    "temp_b": "temperature.b",
    "flow": "flow.primary",
}

UNITS = {
    "temperature.a": "degC",
    "temperature.b": "degC",
    "flow.primary": "L/min",
}


def wide_frame(record: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Fan one frame out into one canonical row per channel."""
    payload = record["row"]
    ts = payload["ts"]

    emitted: set[str] = set()
    for key, channel in CHANNELS.items():
        if key not in payload:
            # A channel absent from this frame is not an error. Instruments drop
            # in and out, and a normalizer that raised on a missing optional
            # channel would discard the channels that *were* present alongside
            # it, because the whole record is skipped when a normalizer raises.
            continue

        if channel in emitted:
            # Defensive, and deliberately loud. Reaching here means CHANNELS
            # maps two payload keys to one channel, which the primary key would
            # otherwise resolve by dropping this row without telling anyone.
            raise ValueError(
                f"channel {channel!r} would be emitted twice from one record; "
                "silver is keyed (row_hash, channel) so the second would be "
                "silently discarded — fix the CHANNELS map"
            )
        emitted.add(channel)

        yield {
            "stream": "example",
            "channel": channel,
            "ts": ts,
            "value": float(payload[key]),
            "unit": UNITS.get(channel, ""),
            "source_class": "measured",
        }
