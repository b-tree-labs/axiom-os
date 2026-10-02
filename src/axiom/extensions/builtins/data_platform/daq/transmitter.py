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
the cursor advances so one bad batch cannot wedge the feed.

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

#: The longest a host may tell us to wait before we use our own schedule
#: instead. A `Retry-After` of a day would silence a site until somebody
#: noticed it had gone quiet, which is the failure mode we are least able to
#: detect.
MAX_HONOURED_RETRY_AFTER_S: float = 3600.0

#: "Not passed", as distinct from "passed as None". `jitter=None` is a
#: deliberate request for a deterministic backoff; omitting it asks for the
#: default, which is random.
_UNSET: object = object()


class Transport(Protocol):
    def post(self, url: str, body: bytes, headers: dict[str, str]):
        """Return ``(status, text)`` or ``(status, text, response_headers)``.

        The third element is optional so that honouring ``Retry-After`` did
        not require every existing transport to change. One that returns two
        values is a transport that never saw a header, which is the honest
        answer for it rather than an empty dict pretending it looked.

        Raise ``OSError`` on transport failure.
        """


def _headers_of(response) -> dict[str, str]:
    """A response's headers as a plain dict, whatever shape it carries them in.

    Never raises: a transport that cannot report its headers must still be
    able to report its status and body, which are the parts delivery depends
    on.
    """
    try:
        raw = getattr(response, "headers", None)
        if raw is None:
            return {}
        return {str(k): str(v) for k, v in dict(raw).items()}
    except Exception:  # noqa: BLE001 - headers are a courtesy, the body is not
        return {}


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
                return (
                    resp.status,
                    resp.read().decode("utf-8", "replace"),
                    _headers_of(resp),
                )
        except urllib.error.HTTPError as exc:
            # The headers matter most on THIS branch: a 429 or 503 is exactly
            # where a host sends `Retry-After`, and urllib delivers that as an
            # exception. Discarding them here is what made honouring the
            # header impossible in production while it worked in every test
            # with a fake transport.
            return exc.code, exc.read().decode("utf-8", "replace"), _headers_of(exc)
        except urllib.error.URLError as exc:
            raise OSError(str(exc.reason)) from exc


@dataclass
class TransmitResult:
    sent: int = 0  # records whose batch got a 2xx
    refused: int = 0  # records dead-lettered on a definitive refusal
    deferred: int = 0  # records left for retry (transport error / 5xx)
    lost: int = 0  # records dropped from the journal before we ever sent them
    retry_after_s: float | None = None
    last_status: int | None = None
    detail: str | None = None
    responses: list[dict] = field(default_factory=list)




def _unpack(answer) -> tuple[int, str, dict[str, str]]:
    """A transport's answer as ``(status, text, headers)``.

    Two elements means a transport that never saw the response headers, which
    is most of them. Returning an empty dict for it is right: it did not look,
    rather than looked and found nothing.
    """
    if isinstance(answer, tuple) and len(answer) >= 3:
        status, text, raw = answer[0], answer[1], answer[2]
        return int(status), str(text), {str(k): str(v) for k, v in dict(raw).items()}
    status, text = answer
    return int(status), str(text), {}


def _rate_limit_window(headers: dict[str, str]):
    """The host's published rate-limit state, through the ONE parser.

    `axiom.infra.ratelimit.parse_headers` already reads `X-RateLimit-Limit`,
    `-Remaining`, `-Reset` and `Retry-After`, matches header names without
    regard to case, and understands the RFC 7231 §7.1.3 HTTP-date form of
    `Retry-After`. It exists because a connector ignored all of those and "the
    first 429 took the whole run down rather than self-pacing through it",
    which is this lane's failure one lane over.

    This briefly had a second parser of its own, seconds-only and without the
    date form. Two implementations of "when does the server want me back" is
    how they stop agreeing, and the one a partner's node runs was the one that
    understood less.
    """
    from axiom.infra.ratelimit import parse_headers

    try:
        return parse_headers(headers)
    except Exception:  # noqa: BLE001 - a header must not break delivery
        return None


def _honoured_retry_after(window) -> float | None:
    """How long the host asked us to wait, capped, or ``None`` if it did not.

    Delegates the PRIORITY to `sleep_for_retry` rather than restating it:
    explicit `Retry-After` seconds first, then `reset_at` — which is where an
    HTTP-date `Retry-After` and `X-RateLimit-Reset` both end up. Restating that
    order here is how the two would come to disagree about which signal wins.

    The sleeper is captured instead of run, because deciding a wait and taking
    it are different jobs and this one only decides. `default_backoff_s=0` is
    what makes "no signal" come back as ``None`` rather than as a number we
    invented.

    Capped because a host asking for a day would silence a site until somebody
    noticed it had gone quiet, which is the failure we are least able to
    detect.
    """
    if window is None:
        return None
    from axiom.infra.ratelimit import sleep_for_retry

    captured: list[float] = []
    try:
        sleep_for_retry(
            window,
            default_backoff_s=0,
            max_backoff_s=int(MAX_HONOURED_RETRY_AFTER_S),
            sleeper=captured.append,
        )
    except Exception:  # noqa: BLE001 - a header must not break delivery
        return None
    seconds = captured[0] if captured else 0.0
    return float(seconds) if seconds > 0 else None


def _parse_body(text: str) -> dict | None:
    """The face's answer as a mapping, or ``None`` when it did not send one.

    ``None`` means "said nothing", which is different from "said zero". Not
    every peer answers with counts, and a bare 200 from one that does not is
    an acknowledgement — refusing to advance on silence would stall every such
    link.
    """
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        return None
    return body if isinstance(body, dict) else None


def _dropped_by_the_face(body: dict | None) -> str:
    """What the face said it dropped, or ``""`` when it kept everything.

    Reads only ``errored``: a batch whose write raised. Deliberately NOT
    ``excluded``, which is a policy gate doing its job — treating that as a
    failure would have a site retrying forever against a rule that is working.
    And deliberately not ``rows_landed < rows_in``, because the shortfall there
    is ``rows_duplicate``, which is at-least-once delivery working as designed.
    """
    if not body:
        return ""
    try:
        errored = int(body.get("errored", 0) or 0)
    except (TypeError, ValueError):
        return ""
    return f"errored={errored}" if errored > 0 else ""


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
        max_partial_retries: int = 5,
        jitter: Callable[[], float] | None = _UNSET,
        extra_metadata: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not face_url or not source or not schema_ref:
            raise ValueError("face_url, source and schema_ref are required")
        # one Journal may carry several feeds with different row shapes:
        # schema_ref is one ref for all, a {feed: ref} map, or a callable
        if isinstance(schema_ref, str):
            self._schema_ref_for: Callable[[str], str] = lambda _stream: schema_ref
        elif callable(schema_ref):
            self._schema_ref_for = schema_ref
        else:
            refs = dict(schema_ref)
            default = refs.get("*")

            def _lookup(feed: str) -> str:
                ref = refs.get(feed, default)
                if not ref:
                    raise KeyError(f"no schema_ref for feed {feed!r}")
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
        #: How many times a 2xx that drops rows is retried before those rows
        #: are dead-lettered. Bounded so a batch the receiver will never
        #: accept cannot pin the cursor and stall every row behind it.
        self.max_partial_retries = max(0, int(max_partial_retries))
        self._partial_failures = 0
        #: Source of randomness for backoff spread, returning [0, 1).
        #: ``None`` disables jitter, which a test wants and a deployment does
        #: not: the default is set here rather than at import so it cannot
        #: freeze one value for the life of the process.
        if jitter is _UNSET:
            import random

            jitter = random.random
        self.jitter = jitter
        #: Monotonic time before which we hold off because the host said its
        #: budget is nearly spent. Zero means no reason to wait.
        self.paced_until: float = 0.0
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
        self.lost_total = 0  # dropped below the cursor before we ever sent them
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
            "feed": first.feed,
            "delivery_class": first.delivery_class,
            "payload_kind": first.payload_kind,
            "source_class": first.source_class,
            **({"model_ref": first.model_ref} if first.model_ref else {}),
            **self.extra_metadata,
        }
        return {
            "item_id": f"{first.producer_id}/{first.feed}/{first.seq}-{last.seq}",
            "schema_ref": self._schema_ref_for(first.feed),
            "rows": [rec.to_row() for _, rec in items],
            "metadata": meta,
        }

    def _request_body(self, items: list[tuple[int, JournaledRecord]]) -> bytes:
        # group by feed so one batch never mixes chains
        groups: dict[tuple[str, str], list[tuple[int, JournaledRecord]]] = {}
        for off, rec in items:
            groups.setdefault((rec.envelope.producer_id, rec.envelope.feed), []).append(
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
        if now < self.paced_until:
            # Pacing is not a failure. The host published a nearly-spent
            # budget on a response it ACCEPTED, so nothing has gone wrong and
            # this must not consume the backoff escalation or mark the
            # connection degraded — it just waits.
            result.retry_after_s = self.paced_until - now
            result.deferred = self.journal.lag(self.cursor_name)
            return result
        start = self.journal.cursor(self.cursor_name)
        # Ask BEFORE reading. read() clamps a stale offset up to the journal's
        # head without telling anyone, so after the read this is zero and the
        # loss is gone from the record. Latched because the instantaneous value
        # stops being true as soon as we catch up, and a spooler that held a
        # long outage must be able to say afterwards what it never sent.
        lost = self.journal.lost(self.cursor_name)
        # Reported by return value and health rather than a log line, which is
        # this package's convention throughout and is the stronger of the two:
        # a caller cannot filter out a field the way a log level can be muted.
        if lost:
            self.lost_total += lost
            result.lost = lost
        items = self.journal.read(start, self.batch_size)
        if not items:
            return result
        next_offset = items[-1][0] + 1
        headers = {"Content-Type": "application/json"}
        token = self._bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            answer = self.transport.post(
                f"{self.face_url}/ingest/rows", self._request_body(items), headers
            )
        except OSError as exc:
            return self._defer(result, len(items), None, f"transport: {exc}")
        status, text, response_headers = _unpack(answer)
        window = _rate_limit_window(response_headers)
        self._observe_budget(window)
        self.last_status = status
        result.last_status = status
        if 200 <= status < 300:
            body = _parse_body(text)
            result.responses.append(body if body is not None else {"raw": text[:200]})
            dropped = _dropped_by_the_face(body)
            if dropped:
                # A 2xx is the receiver saying it HANDLED the request, not
                # that it kept everything in it. The face catches a failing
                # batch write so that one bad batch cannot sink the push,
                # counts it in `errored`, and answers 200 — and those rows are
                # never even added to `rows_in`. Committing here advances past
                # rows the receiver told us, in this same response, that it
                # dropped.
                #
                # Deferred rather than dead-lettered: a write that raised is
                # usually the receiver's disk or database, which is transient,
                # and the rows are still in our journal. `_partial_failures`
                # bounds it so a permanently unacceptable batch cannot pin the
                # cursor forever.
                self._partial_failures += 1
                if self._partial_failures > self.max_partial_retries:
                    self._deadletter(items, status, f"face dropped: {dropped}")
                    self.journal.commit(self.cursor_name, next_offset)
                    self._partial_failures = 0
                    self._failures = 0
                    self.connection = "ok"
                    result.refused = len(items)
                    self.refused_total += len(items)
                    result.detail = (
                        f"face kept dropping these rows ({dropped}); "
                        f"dead-lettered after {self.max_partial_retries} retries"
                    )
                    return result
                return self._defer(result, len(items), status, f"face dropped: {dropped}")

            self.journal.commit(self.cursor_name, next_offset)
            self._failures = 0
            self._partial_failures = 0
            self.connection = "ok"
            result.sent = len(items)
            self.sent_total += len(items)
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
        return self._defer(
            result, len(items), status, text[:500],
            retry_after=_honoured_retry_after(window),
        )

    def _defer(
        self,
        result: TransmitResult,
        n: int,
        status: int | None,
        detail: str,
        *,
        retry_after: float | None = None,
    ) -> TransmitResult:
        step = self.backoff[min(self._failures, len(self.backoff) - 1)]
        if retry_after is not None:
            # The host said when it will be ready. That is an instruction, not
            # an estimate, so it is neither jittered nor overridden — this is
            # the one case where the far end has better information than our
            # table and we were throwing it away.
            delay = retry_after
        else:
            delay = self._jittered(step)
        self._failures += 1
        self._not_before = self._clock() + delay
        self.connection = "degraded"
        result.deferred = n
        result.retry_after_s = delay
        result.last_status = status
        result.detail = detail
        return result

    def _observe_budget(self, window) -> None:
        """Slow down before being refused, if the host published a budget.

        A limit is on every response, not only the one that says no. Waiting
        for a 429 means the link has already been refused once and is about to
        retransmit rows it did not need to.

        `should_throttle` refuses to decide on missing data, which is the right
        default: most faces publish nothing, and a transmitter that throttled
        itself against silence would halve every partner's throughput for no
        reason.
        """
        if window is None:
            return
        try:
            if not window.should_throttle():
                self.paced_until = 0.0
                return
        except Exception:  # noqa: BLE001 - a header must not break delivery
            return
        step = self.backoff[0]
        if window.reset_at is not None:
            try:
                from datetime import UTC, datetime

                step = max(
                    0.0, (window.reset_at - datetime.now(UTC)).total_seconds())
            except Exception:  # noqa: BLE001
                pass
        self.paced_until = self._clock() + min(step, MAX_HONOURED_RETRY_AFTER_S)

    def _jittered(self, step: float) -> float:
        """*step*, spread over (0, step].

        Without this every site that was mid-send when a host went down comes
        back at the same second, and again at the same second, for as long as
        the outage lasts — a synchronised flood at a host that has just
        restarted. Never zero, because a zero wait is a hot loop, and never
        more than the step, because jitter is meant to spread a herd rather
        than extend an outage.
        """
        if self.jitter is None:
            return step
        return step * (1.0 - self.jitter() * 0.999)

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
                            "feed": rec.envelope.feed,
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
            # Latched, so it survives catching up. Non-zero means the feed we
            # delivered is not a complete record of what the source produced.
            "lost_total": self.lost_total,
        }


__all__ = ["DAQTransmitter", "Transport", "TransmitResult", "UrllibTransport"]
