# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Producer — Reader → Consolidator(s) → Journal → {Transmitter, Dispatcher}.

:class:`DAQReader` is the only domain-specific piece: anything with a
``read()`` that yields :class:`ConsolidatedRecord` (a protocol client, a file
tailer, a model's output). Everything after it is generic. The Producer's
step pulls from the Reader, consolidates (which journals), then pumps the
cursors. Nothing reaches a subscriber or the face except through the Journal.

A Reader that fans one upstream message into several feeds (instrument
values, a model's state, a field) yields ``(feed, record)`` pairs and the
Producer routes each to that feed's Consolidator — one chain per feed,
one Journal, one Transmitter.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Protocol

from .consolidator import DAQConsolidator
from .dispatcher import LiveDispatcher
from .envelope import ConsolidatedRecord
from .health import merge_details, silence
from .journal import JournalFull
from .transmitter import DAQTransmitter

Sample = ConsolidatedRecord | tuple[str, ConsolidatedRecord]


class DAQReader(Protocol):
    def read(self) -> Iterable[Sample]:
        """Yield whatever samples are available now (may be empty): a bare
        record for a single-feed Reader, or ``(feed, record)`` pairs."""


class Producer:
    def __init__(
        self,
        *,
        reader: DAQReader,
        consolidator: DAQConsolidator | Mapping[str, DAQConsolidator],
        transmitter: DAQTransmitter | None = None,
        dispatcher: LiveDispatcher | None = None,
        expected_interval_s: float | None = None,
        silence_factor: float = 3.0,
        compact_every: int = 50,
        send_budget_s: float = 0.5,
    ) -> None:
        self.reader = reader
        if isinstance(consolidator, DAQConsolidator):
            self.consolidators: dict[str, DAQConsolidator] = {consolidator.feed: consolidator}
        else:
            self.consolidators = dict(consolidator)
        if not self.consolidators:
            raise ValueError("at least one consolidator is required")
        journals = {id(c.journal) for c in self.consolidators.values()}
        if len(journals) != 1:
            raise ValueError("all consolidators must share one Journal")
        self.transmitter = transmitter
        self.dispatcher = dispatcher
        self.expected_interval_s = expected_interval_s
        self.silence_factor = silence_factor
        self.compact_every = max(0, int(compact_every))
        #: How long one step may keep sending to catch up. A step always makes
        #: at least one request; past this it returns, so the loop still reads.
        self.send_budget_s = max(0.0, float(send_budget_s))
        self._steps = 0
        self.backpressure = False
        self.reader_errors = 0
        self.unrouted = 0
        self.last_error: str | None = None

    @property
    def consolidator(self) -> DAQConsolidator:
        """The single Consolidator (single-feed Producers); the first otherwise."""
        return next(iter(self.consolidators.values()))

    @property
    def journal(self):
        return self.consolidator.journal

    def _route(self, sample: Sample) -> tuple[DAQConsolidator | None, ConsolidatedRecord]:
        if isinstance(sample, tuple):
            feed, rec = sample
            return self.consolidators.get(feed), rec
        if len(self.consolidators) == 1:
            return self.consolidator, sample
        return None, sample

    def step(self) -> dict:
        """One acquisition + fan-out cycle. Never raises for a Reader or
        transport fault — those become health, so the loop keeps going."""
        consumed = journaled = 0
        try:
            samples = list(self.reader.read())
        except Exception as exc:  # noqa: BLE001 — a Reader fault is health, not a crash
            self.reader_errors += 1
            self.last_error = f"reader: {type(exc).__name__}: {exc}"
            samples = []
        # One fsync for the whole read rather than one per reading (group
        # commit): these samples were only in memory until this loop took them.
        batch = self.journal.batch() if hasattr(self.journal, "batch") else contextlib.nullcontext()
        with batch:
            consumed, journaled = self._consume_all(samples)
        out: dict = {"consumed": consumed, "journaled": journaled}
        if self.transmitter is not None:
            out.update(self._send_until_caught_up())
        if self.dispatcher is not None:
            out["dispatched"] = self.dispatcher.pump().delivered
        # retention: once every cursor has moved on, delivered segments go
        self._steps += 1
        if self.compact_every and self._steps % self.compact_every == 0:
            out["compacted"] = self.journal.compact()
        return out

    def _consume_all(self, samples) -> tuple[int, int]:
        consumed = journaled = 0
        for sample in samples:
            consumed += 1
            cons, rec = self._route(sample)
            if cons is None:
                self.unrouted += 1
                self.last_error = "unrouted sample: no consolidator for its feed"
                continue
            try:
                if cons.consume(rec) is not None:
                    journaled += 1
                self.backpressure = False
            except JournalFull as exc:
                # block_producer: stop consuming this cycle; the Reader's
                # samples are not acknowledged past this point
                self.backpressure = True
                self.last_error = f"journal: {exc}"
                break
        return consumed, journaled

    def _send_until_caught_up(self) -> dict:
        """Pump until caught up, told to wait, or out of budget.

        One request per step capped sending at ``batch_size`` per interval
        however far behind the journal was.
        """
        sent = refused = 0
        deferred = 0
        deadline = time.monotonic() + self.send_budget_s
        while True:
            tx = self.transmitter.pump()
            sent += tx.sent
            refused += tx.refused
            deferred = tx.deferred
            if (
                not tx.sent
                or tx.refused
                or getattr(tx, "retry_after_s", None)
                or self.journal.lag(self.transmitter.cursor_name) == 0
                or time.monotonic() >= deadline
            ):
                break
        return {"sent": sent, "refused": refused, "deferred": deferred}

    def run(self, stop: threading.Event, *, interval_s: float = 1.0) -> None:
        while not stop.is_set():
            self.step()
            stop.wait(interval_s)

    def health(self) -> dict:
        parts = [
            self.journal.health_details(),
            self.consolidator.health_details(),
            {
                "feeds": {s: c.health_details() for s, c in self.consolidators.items()},
                "backpressure": self.backpressure,
                "reader_errors": self.reader_errors,
                "unrouted": self.unrouted,
                "last_error": self.last_error,
            },
        ]
        if self.transmitter is not None:
            parts.append({"transmitter": self.transmitter.health_details()})
        if self.dispatcher is not None:
            parts.append({"subscribers": self.dispatcher.health_details()})
        if self.expected_interval_s:
            newest = max(
                (c.last_record_at for c in self.consolidators.values() if c.last_record_at),
                default=None,
            )
            v = silence(
                newest,
                expected_interval_s=self.expected_interval_s,
                factor=self.silence_factor,
            )
            parts.append(
                {"silent": v.silent, "silent_for_s": v.silent_for_s, "silence_reason": v.reason}
            )
        return merge_details(*parts)


def run_forever(producer: Producer, *, interval_s: float = 1.0) -> None:
    """Blocking convenience for a service entry point (Ctrl-C exits cleanly)."""
    stop = threading.Event()
    try:
        producer.run(stop, interval_s=interval_s)
    except KeyboardInterrupt:
        stop.set()
    finally:
        time.sleep(0)


__all__ = ["DAQReader", "Producer", "Sample", "run_forever"]
