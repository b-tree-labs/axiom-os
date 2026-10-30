"""Conform the smoke tests' rows (infra/smoke/push.py) to silver."""

SCHEMA_REF = "smoke/rows-v1"


def rows(record):
    payload = record["row"]
    yield {
        "feed": "smoke",
        "channel": payload["channel"],
        "ts": payload["ts"],
        "value": float(payload["value"]),
        "unit": payload["unit"],
        "source_class": "measured",
    }


#: A collector's own rows (the DAQ transmitter's envelope plus record): one
#: reading per entry of ``values``, so a journaled sample conforms to silver.
DAQ_SCHEMA_REF = "smoke/daq-v1"


def daq_rows(record):
    payload = record["row"]
    for channel, value in dict(payload.get("values") or {}).items():
        yield {
            "feed": payload.get("feed") or "smoke",
            "channel": channel,
            "ts": payload["ts"],
            "value": float(value),
            "unit": "1",
            "source_class": "measured",
        }


def register_all(registry):
    registry.register(SCHEMA_REF, rows)
    registry.register(DAQ_SCHEMA_REF, daq_rows)
