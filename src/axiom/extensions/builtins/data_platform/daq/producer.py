# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Producer — Reader → Consolidator(s) → Journal → {Transmitter, Dispatcher}.

:class:`DAQReader` is the only domain-specific piece: anything with a
``read()`` that yields :class:`ConsolidatedRecord` (a protocol client, a file
tailer, a model's output). Everything after it is generic. The Producer's
step pulls from the Reader, consolidates (which journals), then pumps the
cursors. Nothing reaches a subscriber or the face except through the Journal.

A Reader that fans one upstream message into several streams (instrument
values, a model's state, a field) yields ``(stream, record)`` pairs and the
Producer routes each to that stream's Consolidator — one chain per stream,
one Journal, one Transmitter.
"""

from __future__ import annotations

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
        record for a single-stream Reader, or ``(stream, record)`` pairs."""


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
    ) -> None:
        self.reader = reader
        if isinstance(consolidator, DAQConsolidator):
            self.consolidators: dict[str, DAQConsolidator] = {consolidator.stream: consolidator}
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
        self._steps = 0
        self.backpressure = False
        self.reader_errors = 0
        self.unrouted = 0
        self.last_error: str | None = None

    @property
    def consolidator(self) -> DAQConsolidator:
        """The single Consolidator (single-stream Producers); the first otherwise."""
        return next(iter(self.consolidators.values()))

    @property
    def journal(self):
        return self.consolidator.journal

    def _route(self, sample: Sample) -> tuple[DAQConsolidator | None, ConsolidatedRecord]:
        if isinstance(sample, tuple):
            stream, rec = sample
            return self.consolidators.get(stream), rec
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
        for sample in samples:
            consumed += 1
            cons, rec = self._route(sample)
            if cons is None:
                self.unrouted += 1
                self.last_error = "unrouted sample: no consolidator for its stream"
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
        out: dict = {"consumed": consumed, "journaled": journaled}
        if self.transmitter is not None:
            tx = self.transmitter.pump()
            out["sent"] = tx.sent
            out["refused"] = tx.refused
            out["deferred"] = tx.deferred
        if self.dispatcher is not None:
            out["dispatched"] = self.dispatcher.pump().delivered
        # retention: once every cursor has moved on, delivered segments go
        self._steps += 1
        if self.compact_every and self._steps % self.compact_every == 0:
            out["compacted"] = self.journal.compact()
        return out

    def run(self, stop: threading.Event, *, interval_s: float = 1.0) -> None:
        while not stop.is_set():
            self.step()
            stop.wait(interval_s)

    def health(self) -> dict:
        parts = [
            self.journal.health_details(),
            self.consolidator.health_details(),
            {
                "streams": {s: c.health_details() for s, c in self.consolidators.items()},
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
