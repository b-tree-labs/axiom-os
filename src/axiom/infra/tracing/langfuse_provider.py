# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Langfuse-backed TraceProvider — raw HTTP, no third-party SDK.

The official `langfuse` Python SDK pulls in a `pydantic.v1` shim that
fails to import under Python 3.14 (`unable to infer type for attribute
"description"`). Rather than pin the venv to 3.13 or wait for an
upstream fix, this module speaks LangFuse's public ingestion API
directly via `urllib.request`.

The wire format is the documented `/api/public/ingestion` batch:
each event has an envelope (`id`, `timestamp`, `type`) and a typed
`body` (the entity being created). Authentication is HTTP Basic with
the project's public/secret key pair.

Tests inject a fake transport via the `transport=` kwarg; production
callers pass `public_key`/`secret_key`/`host` and get a default
`_HttpTransport` built for them. Unknown trace ids (i.e., ids the
caller didn't get from `start_trace`) silently no-op so a buggy call
site never brings a user request down. Transport-level failures are
swallowed at flush for the same reason.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _Transport(Protocol):
    def post_batch(self, events: list[dict[str, Any]]) -> None: ...


class _HttpTransport:
    """Default transport: HTTP POST to {host}/api/public/ingestion."""

    def __init__(
        self, *, public_key: str, secret_key: str, host: str, timeout: float = 10.0
    ) -> None:
        self._url = f"{host.rstrip('/')}/api/public/ingestion"
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._auth = f"Basic {token}"
        self._timeout = timeout

    def post_batch(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        payload = json.dumps({"batch": events}).encode()
        req = urllib.request.Request(
            self._url,
            data=payload,
            headers={
                "Authorization": self._auth,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            if resp.status >= 400:
                raise urllib.error.HTTPError(
                    self._url, resp.status, resp.reason, resp.headers, None
                )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


class LangfuseTraceProvider:
    """TraceProvider that buffers events and flushes them to LangFuse via HTTP."""

    def __init__(
        self,
        *,
        transport: _Transport | None = None,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
    ) -> None:
        if transport is None:
            if not (public_key and secret_key and host):
                raise ValueError(
                    "LangfuseTraceProvider requires either `transport=` or all "
                    "of `public_key=`, `secret_key=`, `host=`"
                )
            transport = _HttpTransport(
                public_key=public_key, secret_key=secret_key, host=host
            )
        self._transport = transport
        self._buffer: list[dict[str, Any]] = []
        self._known_trace_ids: set[str] = set()

    def start_trace(self, name: str, **metadata: Any) -> str:
        trace_id = _new_id()
        self._known_trace_ids.add(trace_id)
        self._buffer.append(
            {
                "id": _new_id(),
                "timestamp": _now_iso(),
                "type": "trace-create",
                "body": {
                    "id": trace_id,
                    "name": name,
                    "metadata": dict(metadata),
                },
            }
        )
        return trace_id

    def log_generation(
        self,
        trace_id: str,
        *,
        model: str,
        prompt: Any,
        output: Any,
        **metadata: Any,
    ) -> None:
        if trace_id not in self._known_trace_ids:
            return
        now = _now_iso()
        self._buffer.append(
            {
                "id": _new_id(),
                "timestamp": now,
                "type": "generation-create",
                "body": {
                    "id": _new_id(),
                    "traceId": trace_id,
                    "name": "generation",
                    "model": model,
                    "input": prompt,
                    "output": output,
                    "startTime": now,
                    "metadata": dict(metadata),
                },
            }
        )

    def log_retrieval(
        self,
        trace_id: str,
        *,
        query: str,
        results: list[Any],
        **metadata: Any,
    ) -> None:
        if trace_id not in self._known_trace_ids:
            return
        now = _now_iso()
        self._buffer.append(
            {
                "id": _new_id(),
                "timestamp": now,
                "type": "span-create",
                "body": {
                    "id": _new_id(),
                    "traceId": trace_id,
                    "name": "retrieval",
                    "input": {"query": query},
                    "output": {"results": results},
                    "startTime": now,
                    "metadata": dict(metadata),
                },
            }
        )

    def score(
        self, trace_id: str, *, name: str, value: float, **metadata: Any
    ) -> None:
        if trace_id not in self._known_trace_ids:
            return
        self._buffer.append(
            {
                "id": _new_id(),
                "timestamp": _now_iso(),
                "type": "score-create",
                "body": {
                    "id": _new_id(),
                    "traceId": trace_id,
                    "name": name,
                    "value": value,
                    "metadata": dict(metadata),
                },
            }
        )

    def flush(self) -> None:
        events, self._buffer = self._buffer, []
        try:
            self._transport.post_batch(events)
        except Exception:  # noqa: BLE001
            # A buggy network must never bring a user request down.
            logger.warning("langfuse flush failed; dropping %d events", len(events))
