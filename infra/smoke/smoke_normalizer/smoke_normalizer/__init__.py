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


def register_all(registry):
    registry.register(SCHEMA_REF, rows)
