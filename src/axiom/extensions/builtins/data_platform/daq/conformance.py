# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Does this Reader satisfy the DAQ contract? Run it and find out.

A contract with one implementation is a guess. :class:`DAQReader` has been
implemented essentially once, and the next three are being written by people
who do not work on this codebase — an EPICS reader at a partner site, a PXI
reader for an in-core experiment. Handing them a Protocol and good intentions
is how a contract turns out to have meant something else.

So this is the contract, executable. It runs against a live Reader with no
database, no ingest face and no reactor, and every failure says what the core
does with the thing it found — because "invalid" teaches nothing and "the
Producer will busy-loop on this" teaches the shape.

**The same suite gates both consequence levels.** A stream feeding a dashboard
and a stream a control law is credited to read differ in what happens when they
go quiet (``silence`` alerts, :class:`~.health.CreditedGuard` trips), not in
what a Reader must do. Getting the same answer from the same checks is the
point: the low-consequence site is the proving ground for the high-consequence
one, which is only true if they are held to one standard.

Usage — the whole kit::

    from axiom.extensions.builtins.data_platform.daq.conformance import verify_reader

    report = verify_reader(MyReader(...))
    print("\\n".join(report.lines))
    assert report.ok, report.failures
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .envelope import ConsolidatedRecord

#: Strings that look like a value and are not one. A Reader that cannot
#: determine a unit must leave it out: an absent unit is visible and the
#: platform declines to chart it, while "unknown" flows downstream looking
#: like data. Learned from a channel map where 48 of 119 channels had no unit
#: and the join invented one rather than admitting it.
PLACEHOLDER_VALUES = frozenset(
    {"", "-", "--", "n/a", "na", "none", "null", "unknown", "tbd", "?", "nan"}
)

#: How long a single ``read()`` may take before the Producer's loop is starved.
#: A Reader blocks waiting for data at its own peril: the loop also pumps the
#: journal, the transmitter and the health probes, so a blocking read stops
#: the staleness detection that exists to notice it.
READ_BUDGET_S = 2.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    #: What the core does with a Reader that fails this. Not decoration —
    #: it is the difference between a rule and an arbitrary rejection.
    consequence: str = ""

    @property
    def line(self) -> str:
        mark = "✓" if self.ok else "✗"
        out = f"  {mark} {self.name}: {self.detail}"
        if not self.ok and self.consequence:
            out += f"\n      → {self.consequence}"
        return out


@dataclass
class ConformanceReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def lines(self) -> list[str]:
        head = (
            f"{sum(1 for c in self.checks if c.ok)}/{len(self.checks)} checks passed"
            if self.checks
            else "no checks ran — that is a failure, not a pass"
        )
        return [head, *(c.line for c in self.checks)]


def _samples(raw: Iterable[Any]) -> list[tuple[str | None, Any]]:
    """Normalise whatever ``read()`` yielded into ``(stream, record)`` pairs."""
    out: list[tuple[str | None, Any]] = []
    for item in raw:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
            out.append((item[0], item[1]))
        else:
            out.append((None, item))
    return out


def _drain(reader: Any, *, budget_s: float = READ_BUDGET_S) -> tuple[list, float, Exception | None]:
    started = time.monotonic()
    try:
        raw = list(reader.read())
    except Exception as exc:  # noqa: BLE001 — the point is to report it
        return [], time.monotonic() - started, exc
    return raw, time.monotonic() - started, None


def verify_reader(
    reader: Any,
    *,
    reads: int = 3,
    budget_s: float = READ_BUDGET_S,
    quiesce: Callable[[], None] | None = None,
) -> ConformanceReport:
    """Run the contract against a live *reader*.

    ``reads`` — how many times to pump it. More than one on purpose: a Reader
    that re-yields everything it has ever seen passes a single read and floods
    the journal on the second.

    ``quiesce`` — optional hook called before the final read, for a Reader
    whose upstream can be silenced. Without it the empty-read check is still
    attempted, since most Readers go quiet on their own between samples.
    """
    report = ConformanceReport()
    add = report.checks.append

    # --- the interface itself ------------------------------------------------
    if not callable(getattr(reader, "read", None)):
        add(
            Check(
                "read_exists",
                False,
                f"{type(reader).__name__} has no callable read()",
                "the Producer calls reader.read() every loop; nothing else is required",
            )
        )
        return report
    add(Check("read_exists", True, "read() is callable"))

    batches: list[list] = []
    seen_hashes: set[str] = set()
    duplicate: str | None = None
    slow: float | None = None

    for i in range(max(1, reads)):
        if quiesce is not None and i == max(1, reads) - 1:
            quiesce()
        raw, elapsed, exc = _drain(reader, budget_s=budget_s)
        if exc is not None:
            add(
                Check(
                    "read_does_not_raise",
                    False,
                    f"read() raised {type(exc).__name__}: {exc}",
                    "the Producer loop has no new samples AND no health signal — "
                    "an upstream that can fail must be counted, not thrown "
                    "(parse-failure counters are what make a silent decode break loud)",
                )
            )
            return report
        if elapsed > budget_s:
            slow = elapsed
        batches.append(raw)

    add(Check("read_does_not_raise", True, f"{reads} read(s) completed without raising"))
    add(
        Check(
            "read_within_budget",
            slow is None,
            "each read() returned promptly"
            if slow is None
            else f"a read() took {slow:.1f}s (budget {budget_s}s)",
            "the same loop pumps the journal, transmitter and staleness probes — "
            "a blocking read stops the detection that exists to notice it",
        )
    )

    flat = [p for b in batches for p in _samples(b)]

    add(
        Check(
            "empty_read_is_legal",
            any(len(b) == 0 for b in batches) or len(flat) > 0,
            "read() returns an empty iterable when there is nothing new"
            if any(len(b) == 0 for b in batches)
            else "every read() produced samples (could not observe a quiet read)",
            "a Reader with no new data must return empty, never raise or block",
        )
    )

    if not flat:
        add(
            Check(
                "yields_samples",
                False,
                "no samples in any read — cannot verify record shape",
                "feed the Reader something before running this, or pass a fixture "
                "upstream; a kit that passes on silence proves nothing",
            )
        )
        return report
    add(Check("yields_samples", True, f"{len(flat)} sample(s) observed"))

    # --- what it yields ------------------------------------------------------
    bad_type = [r for _, r in flat if not isinstance(r, ConsolidatedRecord)]
    add(
        Check(
            "samples_are_consolidated_records",
            not bad_type,
            "every sample is a ConsolidatedRecord"
            if not bad_type
            else f"{len(bad_type)} sample(s) are {type(bad_type[0]).__name__}, not ConsolidatedRecord",
            "the Consolidator reads .schema_id/.ts/.values off the record; a dict "
            "or a bare tuple fails deeper in, where the cause is no longer visible",
        )
    )
    records = [r for _, r in flat if isinstance(r, ConsolidatedRecord)]
    if not records:
        return report

    # --- timestamps ----------------------------------------------------------
    unparsed, naive = [], []
    for r in records:
        try:
            dt = datetime.fromisoformat(str(r.ts).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            unparsed.append(r.ts)
            continue
        if dt.tzinfo is None:
            naive.append(r.ts)
    add(
        Check(
            "timestamps_parse",
            not unparsed,
            "every ts is ISO-8601"
            if not unparsed
            else f"{len(unparsed)} unparseable, e.g. {unparsed[0]!r}",
            "staleness is computed by subtracting ts from now; a ts that will not "
            "parse reads as 'no timestamp', which the guards treat as absent",
        )
    )
    add(
        Check(
            "timestamps_are_tz_aware",
            not naive,
            "every ts carries an offset"
            if not naive
            else f"{len(naive)} naive, e.g. {naive[0]!r}",
            "a naive ts is compared against UTC — an hour of DST error becomes an "
            "hour of apparent staleness, or an hour of apparent freshness",
        )
    )

    # --- identity and duplication -------------------------------------------
    no_schema = [r for r in records if not str(r.schema_id).strip()]
    add(
        Check(
            "records_declare_schema_id",
            not no_schema,
            "every record declares a schema_id"
            if not no_schema
            else f"{len(no_schema)} record(s) have an empty schema_id",
            "schema_id selects the normalizer; an empty one cannot be routed and "
            "is also how a simulated source ends up labelled as measurement",
        )
    )

    for r in records:
        h = f"{r.schema_id}|{r.ts}|{r.record_hash()}"
        if h in seen_hashes:
            duplicate = h
            break
        seen_hashes.add(h)
    add(
        Check(
            "no_duplicate_resend",
            duplicate is None,
            "no sample was yielded twice across reads"
            if duplicate is None
            else "the same (schema_id, ts, values) came back on a later read()",
            "a Reader that re-yields its whole buffer floods the journal and makes "
            "every stuck-value probe fire; read() returns what is NEW",
        )
    )

    # --- values --------------------------------------------------------------
    empty_values = [r for r in records if not r.values]
    add(
        Check(
            "records_carry_values",
            not empty_values,
            "every record carries at least one value"
            if not empty_values
            else f"{len(empty_values)} record(s) have no values",
            "an empty record still advances the clock, so a source emitting them "
            "looks alive while delivering nothing",
        )
    )

    placeholders: list[str] = []
    for r in records:
        for k, v in dict(r.values).items():
            if isinstance(v, str) and v.strip().lower() in PLACEHOLDER_VALUES:
                placeholders.append(f"{k}={v!r}")
        for k, v in dict(r.tags).items():
            if isinstance(v, str) and v.strip().lower() in PLACEHOLDER_VALUES:
                placeholders.append(f"tag {k}={v!r}")
    add(
        Check(
            "no_placeholder_values",
            not placeholders,
            "no placeholder strings in values or tags"
            if not placeholders
            else f"{len(placeholders)} placeholder(s), e.g. {placeholders[0]}",
            "omit what you do not know. An absent unit is visible and the platform "
            "declines to chart it; 'unknown' flows downstream looking like data",
        )
    )

    # --- optional surfaces ---------------------------------------------------
    if hasattr(reader, "health"):
        try:
            h = reader.health()
            ok = isinstance(h, dict)
            add(
                Check(
                    "health_returns_a_dict",
                    ok,
                    "health() returns a dict of counters"
                    if ok
                    else f"health() returned {type(h).__name__}",
                    "the core merges provider counters into the stream's health "
                    "detail; anything else is dropped silently",
                )
            )
        except Exception as exc:  # noqa: BLE001
            add(
                Check(
                    "health_returns_a_dict",
                    False,
                    f"health() raised {type(exc).__name__}: {exc}",
                    "health() is polled on the loop — it must never raise",
                )
            )
    return report


__all__ = [
    "PLACEHOLDER_VALUES",
    "READ_BUDGET_S",
    "Check",
    "ConformanceReport",
    "verify_reader",
]
