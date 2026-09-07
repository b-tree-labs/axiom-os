# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Transmitter — a cursor into the Journal that POSTs to the ingest face.

Batches of journaled records become one ``POST /ingest/rows`` request
(``{source, batches:[{item_id, schema_ref, rows, metadata}]}``). Delivery is
at-least-once: the cursor advances only after a 2xx, so a crash or a 5xx
replays the batch, and the face's row ``content_hash`` dedup lands it once.
A definitive refusal (401/403/422) is not retried forever — the batch goes to
``deadletter.jsonl`` next to the Journal, the refusal is counted in health, and
the cursor advances so one bad batch cannot wedge the stream.

The request never carries ``site``: the face derives it from the credential.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .envelope import JournaledRecord, canonical_json
from .journal import DAQJournal

FORBIDDEN_METADATA = frozenset({"site"})
DEFAULT_BACKOFF: tuple[float, ...] = (1, 2, 4, 8, 16, 32, 60)


class Transport(Protocol):
    def post(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
        """Return ``(status, response_text)``; raise ``OSError`` on transport failure."""


class UrllibTransport:
    """HTTP(S) POST with certificate verification always on.

    ``ca_bundle`` names an additional trust anchor — a PEM file holding the
    CA (or self-signed certificate) that issued the face's certificate. A
    producer at one institution pushing to another's ingest face is the
    normal case, and that face is often behind an institutional or private
    CA; trusting it explicitly is how a deployment stays verified instead of
    reaching for an unverified context.

    There is deliberately no "skip verification" option. A producer that
    cannot verify its face should fail loudly and be fixed, because the
    alternative silently accepts anyone who can answer on that address.
    """

    def __init__(
        self, *, timeout_s: float = 10.0, ca_bundle: str | os.PathLike[str] | None = None
    ) -> None:
        self.timeout_s = timeout_s
        self.ca_bundle = str(ca_bundle) if ca_bundle else None
        self._opener: urllib.request.OpenerDirector | None = None
        if self.ca_bundle:
            path = Path(self.ca_bundle).expanduser()
            if not path.is_file():
                raise ValueError(f"ca_bundle does not exist: {path}")
            try:
                context = ssl.create_default_context(cafile=str(path))
            except ssl.SSLError as exc:
                raise ValueError(f"ca_bundle {path} is not a usable PEM: {exc}") from exc
            self.ca_bundle = str(path)
            self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

    def post(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        opener = self._opener.open if self._opener is not None else urllib.request.urlopen
        try:
            with opener(req, timeout=self.timeout_s) as resp:  # noqa: S310
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise OSError(str(exc.reason)) from exc


@dataclass
class TransmitResult:
    sent: int = 0  # records whose batch got a 2xx
    refused: int = 0  # records dead-lettered on a definitive refusal
    deferred: int = 0  # records left for retry (transport error / 5xx)
    retry_after_s: float | None = None
    last_status: int | None = None
    detail: str | None = None
    responses: list[dict] = field(default_factory=list)


class DAQTransmitter:
    def __init__(
        self,
        *,
        journal: DAQJournal,
        face_url: str,
        source: str,
        schema_ref: str | Mapping[str, str] | Callable[[str], str],
        token: str | None = None,
        token_file: str | os.PathLike[str] | None = None,
        transport: Transport | None = None,
        batch_size: int = 200,
        cursor_name: str = "transmitter",
        backoff: tuple[float, ...] = DEFAULT_BACKOFF,
        extra_metadata: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not face_url or not source or not schema_ref:
            raise ValueError("face_url, source and schema_ref are required")
        # one Journal may carry several streams with different row shapes:
        # schema_ref is one ref for all, a {stream: ref} map, or a callable
        if isinstance(schema_ref, str):
            self._schema_ref_for: Callable[[str], str] = lambda _stream: schema_ref
        elif callable(schema_ref):
            self._schema_ref_for = schema_ref
        else:
            refs = dict(schema_ref)
            default = refs.get("*")

            def _lookup(stream: str) -> str:
                ref = refs.get(stream, default)
                if not ref:
                    raise KeyError(f"no schema_ref for stream {stream!r}")
                return ref

            self._schema_ref_for = _lookup
        bad = FORBIDDEN_METADATA & set(extra_metadata or {})
        if bad:
            raise ValueError(f"metadata may not carry {sorted(bad)} — the face derives it")
        self.journal = journal
        self.face_url = face_url.rstrip("/")
        self.source = source
        self.schema_ref = schema_ref
        self._token = token
        self._token_file = Path(token_file) if token_file else None
        self.transport = transport or UrllibTransport()
        self.batch_size = max(1, int(batch_size))
        self.cursor_name = cursor_name
        self.backoff = tuple(backoff) or DEFAULT_BACKOFF
        self.extra_metadata = dict(extra_metadata or {})
        self._clock = clock
        self._failures = 0
        self._not_before: float = 0.0
        self.connection = "idle"  # idle | ok | degraded
        self.last_status: int | None = None
        self.refused_total = 0
        self.sent_total = 0
        self.deadletter_path = journal.root / "deadletter.jsonl"

    # -- credential ---------------------------------------------------------

    def _bearer(self) -> str | None:
        if self._token_file is not None:
            return self._token_file.read_text().strip() or None
        return self._token

    # -- batch shaping --------------------------------------------------------

    def _batch(self, items: list[tuple[int, JournaledRecord]]) -> dict:
        first, last = items[0][1].envelope, items[-1][1].envelope
        meta = {
            "producer_id": first.producer_id,
            "stream": first.stream,
            "delivery_class": first.delivery_class,
            "payload_kind": first.payload_kind,
            "source_class": first.source_class,
            **({"model_ref": first.model_ref} if first.model_ref else {}),
            **self.extra_metadata,
        }
        return {
            "item_id": f"{first.producer_id}/{first.stream}/{first.seq}-{last.seq}",
            "schema_ref": self._schema_ref_for(first.stream),
            "rows": [rec.to_row() for _, rec in items],
            "metadata": meta,
        }

    def _request_body(self, items: list[tuple[int, JournaledRecord]]) -> bytes:
        # group by stream so one batch never mixes chains
        groups: dict[tuple[str, str], list[tuple[int, JournaledRecord]]] = {}
        for off, rec in items:
            groups.setdefault((rec.envelope.producer_id, rec.envelope.stream), []).append(
                (off, rec)
            )
        body = {"source": self.source, "batches": [self._batch(g) for g in groups.values()]}
        return canonical_json(body)

    # -- pump ---------------------------------------------------------------

    def pump(self) -> TransmitResult:
        """Send what the cursor has not yet delivered. One request per call at
        most; call again to drain. Honours the backoff window after a failure."""
        result = TransmitResult()
        now = self._clock()
        if now < self._not_before:
            result.retry_after_s = self._not_before - now
            result.deferred = self.journal.lag(self.cursor_name)
            return result
        start = self.journal.cursor(self.cursor_name)
        items = self.journal.read(start, self.batch_size)
        if not items:
            return result
        next_offset = items[-1][0] + 1
        headers = {"Content-Type": "application/json"}
        token = self._bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            status, text = self.transport.post(
                f"{self.face_url}/ingest/rows", self._request_body(items), headers
            )
        except OSError as exc:
            return self._defer(result, len(items), None, f"transport: {exc}")
        self.last_status = status
        result.last_status = status
        if 200 <= status < 300:
            self.journal.commit(self.cursor_name, next_offset)
            self._failures = 0
            self.connection = "ok"
            result.sent = len(items)
            self.sent_total += len(items)
            try:
                result.responses.append(json.loads(text))
            except ValueError:
                result.responses.append({"raw": text[:200]})
            return result
        if status in (400, 401, 403, 404, 413, 422):
            # definitive: dead-letter and move on, loudly counted
            self._deadletter(items, status, text)
            self.journal.commit(self.cursor_name, next_offset)
            self.connection = "ok"
            result.refused = len(items)
            self.refused_total += len(items)
            result.detail = text[:500]
            return result
        return self._defer(result, len(items), status, text[:500])

    def _defer(
        self, result: TransmitResult, n: int, status: int | None, detail: str
    ) -> TransmitResult:
        delay = self.backoff[min(self._failures, len(self.backoff) - 1)]
        self._failures += 1
        self._not_before = self._clock() + delay
        self.connection = "degraded"
        result.deferred = n
        result.retry_after_s = delay
        result.last_status = status
        result.detail = detail
        return result

    def _deadletter(self, items: list[tuple[int, JournaledRecord]], status: int, text: str) -> None:
        with self.deadletter_path.open("ab") as fh:
            for off, rec in items:
                fh.write(
                    canonical_json(
                        {
                            "offset": off,
                            "status": status,
                            "detail": text[:500],
                            "producer_id": rec.envelope.producer_id,
                            "stream": rec.envelope.stream,
                            "seq": rec.envelope.seq,
                            "content_hash": rec.envelope.content_hash,
                        }
                    )
                    + b"\n"
                )

    def health_details(self) -> dict:
        return {
            "connection": self.connection,
            "last_status": self.last_status,
            "consecutive_failures": self._failures,
            "cursor": self.journal.cursor(self.cursor_name),
            "lag": self.journal.lag(self.cursor_name),
            "sent_total": self.sent_total,
            "refused_total": self.refused_total,
        }


__all__ = ["DAQTransmitter", "Transport", "TransmitResult", "UrllibTransport"]
