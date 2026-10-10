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
    #: Send times of beats that could not be delivered, kept until one gets
    #: through: a day of beats at the one-a-minute default.
    MAX_UNDELIVERED = 1440

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
        self._undelivered: list[str] = []

    def due(self) -> bool:
        return self._last is None or self._clock() - self._last >= self.interval_s

    def send(self, beat: dict[str, Any]) -> bool:
        """Post one beat now. True when the face accepted it."""
        self._last = self._clock()
        from datetime import UTC, datetime

        # When it was sent, by the node's clock; and the beats that never
        # arrived. Together with when this one arrives, they tell an outage of
        # the link from an outage of the node (ADR-182 D5a).
        sent_at = datetime.now(UTC).isoformat()
        beat = {**beat, "sent_at": beat.get("sent_at", sent_at)}
        # Where the front door can reach this node, so it can check it from the
        # outside. A push-only node sets no public address and is not checked.
        import os

        public = os.environ.get("AXIOM_PUBLIC_URL", "").strip().rstrip("/")
        if public and "probe_url" not in beat:
            beat["probe_url"] = public
        if self._undelivered:
            beat["undelivered_sent_at"] = list(self._undelivered)
        if "maintenance" not in beat:
            from axiom.infra.maintenance import heartbeat_section

            section = heartbeat_section()
            if section is not None:
                beat = {**beat, "maintenance": section}
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
            self._remember(beat["sent_at"])
            return False
        self.last_status = status
        ok = 200 <= status < 300
        self.last_error = "" if ok else f"HTTP {status}: {text[:160]}"
        if ok:
            self._undelivered = []
        else:
            self._remember(beat["sent_at"])
        return ok

    def _remember(self, sent_at: str) -> None:
        if sent_at not in self._undelivered:
            self._undelivered.append(sent_at)
        del self._undelivered[: -self.MAX_UNDELIVERED]


__all__ = ["HeartbeatSender"]
