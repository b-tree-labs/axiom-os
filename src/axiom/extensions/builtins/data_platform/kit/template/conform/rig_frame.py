"""Bronze to silver: turn one raw rig frame into canonical rows.

This is the example normalizer. Copy it, rename it, and change three things:
SCHEMA_REF (what your raw records say they are), CHANNELS (your field names to
the channel names you want in silver), and UNITS.

A normalizer is one pure function of one dict: no database, no network, no
platform import. That is what lets you test it with nothing installed (see
tests/) and what lets the platform run it on your data later.

Four rules, each because the quiet alternative fails:

* Do not set site, schema_ref or row_hash; the platform fills them in.
* An absent optional channel is a skip, not a raise: raising drops the whole
  record, including the channels that were present.
* Timestamps carry a timezone.
* Never emit one channel twice from one record: silver keeps the first and
  drops the second without a word. This function raises instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SCHEMA_REF = "example/rig-frame-v1"

#: Raw field -> silver channel name.
CHANNELS = {
    "t_in": "temperature.inlet",
    "t_out": "temperature.outlet",
    "flow": "flow.primary",
}

#: Silver channel -> unit. A bare number is not a measurement.
UNITS = {
    "temperature.inlet": "degC",
    "temperature.outlet": "degC",
    "flow.primary": "L/min",
}


def rig_frame(record: dict[str, Any]) -> Iterable[dict[str, Any]]:
    payload = record["row"]
    ts = payload["ts"]  # a frame with no time is broken; let it raise
    emitted: set[str] = set()
    for field, channel in CHANNELS.items():
        value = payload.get(field)
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if channel in emitted:
            raise ValueError(f"channel {channel!r} would be emitted twice from one record")
        emitted.add(channel)
        yield {
            "feed": "rig",
            "channel": channel,
            "ts": ts,
            "value": float(value),
            "unit": UNITS[channel],
            "source_class": "measured",
        }


def register_all(registry) -> None:
    registry.register(SCHEMA_REF, rig_frame)
