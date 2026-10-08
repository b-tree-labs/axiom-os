# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A sending node's heartbeat, posted on the transmitter's own path.

The platform keeps it per site and node (``ingest_sink.heartbeat``), so a
collector between runs is seen as alive rather than gone. The beat goes to the
same face with the same credential as the data, so it proves the whole path,
not only that the process is up. It is small, at most once a minute, and a
failure to send it never touches the data.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from ..ingest_sink.heartbeat import HEARTBEAT_SCHEMA
from .transmitter import Transport, UrllibTransport, _unpack


class HeartbeatSender:
    def __init__(
        self,
        *,
        face_url: str,
        source: str,
        bearer: Callable[[], str | None],
        transport: Transport | None = None,
        interval_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.face_url = face_url.rstrip("/")
        self.source = source
        self._bearer = bearer
        self.transport = transport or UrllibTransport()
        self.interval_s = max(10.0, float(interval_s))
        self._clock = clock
        self._last: float | None = None
        self.last_status: int | None = None
        self.last_error = ""

    def due(self) -> bool:
        return self._last is None or self._clock() - self._last >= self.interval_s

    def send(self, beat: dict[str, Any]) -> bool:
        """Post one beat now. True when the face accepted it."""
        self._last = self._clock()
        body = json.dumps(
            {
                "source": self.source,
                "batches": [
                    {
                        "item_id": f"hb-{beat.get('node', 'node')}-{int(time.time())}",
                        "schema_ref": HEARTBEAT_SCHEMA,
                        "rows": [beat],
                    }
                ],
            },
            default=str,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = self._bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            status, text, _ = _unpack(self.transport.post(f"{self.face_url}/ingest/rows", body, headers))
        except OSError as exc:
            self.last_status, self.last_error = None, f"transport: {exc}"
            return False
        self.last_status = status
        self.last_error = "" if 200 <= status < 300 else f"HTTP {status}: {text[:160]}"
        return 200 <= status < 300


__all__ = ["HeartbeatSender"]
