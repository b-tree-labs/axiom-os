# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An edge node's process in a dev topology: a labelled sample source on the platform's DAQ.

Nothing here is a second transport. The reading path is the platform's own:
:class:`~axiom.extensions.builtins.data_platform.daq.producer.Producer`
journals each reading and :class:`~...daq.transmitter.DAQTransmitter` sends it
store-and-forward to the home's ``/ingest/rows`` (at-least-once, deduplicated
by the intake). This file adds only the sample reader, the fault switches and
a status file ``dev status`` reads.

Every reading is labelled sample: ``tags.sample = "true"``, ``source_class =
simulated``, and its index ``tags.reading`` so a check can count each one.

Faults are read from ``fault.json`` beside the config on every tick:
``source-offline`` (the reader yields nothing), ``slow-link`` (each send waits
``slow_link_s``), ``clock-skew`` (timestamps shift by ``skew_s``). A home that
is down is not this process's business: the transmitter holds and retries.

Run as ``python -m axiom.extensions.builtins.dev.edge_runner <config.json>``;
the key is read from ``$DEV_EDGE_TOKEN`` and never written anywhere.
"""

from __future__ import annotations

import json
import math
import os
import signal
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TOKEN_ENV = "DEV_EDGE_TOKEN"
FAULT_NAMES = ("source-offline", "slow-link", "clock-skew")
SCHEMA = "dev/sample-v1"


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class SampleReader:
    """The node's channels at ``rate_hz``, one record per tick carrying every channel."""

    def __init__(
        self, *, channels: list[str], rate_hz: float, count: int, faults, state: dict, run: str = ""
    ):
        from axiom.extensions.builtins.data_platform.daq import ConsolidatedRecord

        self._record = ConsolidatedRecord
        self.channels, self.period = channels, 1.0 / max(rate_hz, 0.1)
        self.count, self.faults, self.state, self.run = count, faults, state, run
        self._next = time.monotonic()

    def read(self):
        f = self.faults()
        if f.get("source-offline"):
            self._next = time.monotonic()
            return []
        out = []
        now = time.monotonic()
        while self._next <= now and (not self.count or self.state["read"] < self.count):
            i = self.state["read"]
            ts = datetime.now(UTC) + timedelta(seconds=float(f.get("skew_s") or 0))
            values = {
                c: round(20 + k + 5 * math.sin(i / 10 + k), 4) for k, c in enumerate(self.channels)
            }
            out.append(
                (
                    "sample",
                    self._record(
                        schema_id=SCHEMA,
                        ts=ts.isoformat(),
                        values=values,
                        tags={
                            "sample": "true",
                            "reading": str(i),
                            **({"run": self.run} if self.run else {}),
                        },
                    ),
                )
            )
            self.state["read"] = i + 1
            self._next += self.period
        return out


class SlowTransport:
    """The platform's transport, with the slow-link fault applied before each send."""

    def __init__(self, inner, faults):
        self.inner, self.faults = inner, faults

    def post(self, url, body, headers):
        delay = float(self.faults().get("slow_link_s") or 0)
        if delay:
            time.sleep(delay)
        return self.inner.post(url, body, headers)


def main(argv: list[str]) -> int:
    from axiom.extensions.builtins.data_platform.daq import DAQJournal
    from axiom.extensions.builtins.data_platform.daq.consolidator import DAQConsolidator
    from axiom.extensions.builtins.data_platform.daq.producer import Producer
    from axiom.extensions.builtins.data_platform.daq.transmitter import (
        DAQTransmitter,
        UrllibTransport,
    )

    cfg_path = Path(argv[0])
    cfg = read_json(cfg_path)
    home = cfg_path.parent
    fault_file, status_file = home / "fault.json", home / "edge-status.json"
    run = str(cfg.get("run") or "")
    # A counted run starts at reading 0 in a journal of its own; otherwise resume.
    state = {"read": 0 if run else int(read_json(status_file).get("read") or 0)}

    def faults() -> dict:
        return read_json(fault_file)

    journal = DAQJournal(home / str(cfg.get("journal") or "journal"))
    cons = DAQConsolidator(
        journal=journal,
        producer_id=cfg["producer_id"],
        feed="sample",
        source_class="simulated",
        model_ref="dev.sample-source",
    )
    tx = DAQTransmitter(
        journal=journal,
        face_url=cfg["face_url"],
        source=cfg["source"],
        schema_ref=SCHEMA,
        token_provider=lambda: os.environ.get(TOKEN_ENV) or None,
        transport=SlowTransport(UrllibTransport(timeout_s=5), faults),
        backoff=(0.5, 1, 2, 2),
    )
    producer = Producer(
        reader=SampleReader(
            channels=cfg["channels"],
            rate_hz=float(cfg["rate_hz"]),
            count=int(cfg.get("count") or 0),
            faults=faults,
            state=state,
            run=run,
        ),
        consolidator=cons,
        transmitter=tx,
        send_budget_s=0.5,
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        producer.step()
        h = tx.health_details()
        write_json(
            status_file,
            {
                "read": state["read"],
                "count": int(cfg.get("count") or 0),
                "lag": h["lag"],
                "sent_total": h["sent_total"],
                "connection": h["connection"],
                "last_status": h["last_status"],
                "key_refused": h["key_refused"],
                "faults": sorted(k for k, v in faults().items() if v and k in FAULT_NAMES),
                "face_url": cfg["face_url"],
                "at": time.time(),
            },
        )
        stop.wait(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
