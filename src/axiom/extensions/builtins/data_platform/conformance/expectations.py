# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Conforming expectations: named, versioned checks a site DECLARES on its rows.

## What this is

The bronze→silver hop already has one place that judges a reading: the
VALIDATE stage (:func:`..validating_upsert`, :mod:`..company`), which looks at a
whole instant because the faults it hunts only show up across channels. Many
defects need no company at all. A reading that is exactly a hardware "no
reading" word is wrong on its own, whatever its neighbours say.

An *expectation* is that second kind of check, built so it can be reused:

- it has a **name** and a **version** and lives in one catalogue
  (:data:`CATALOGUE`), so a contributor can discover it (``axi data
  expectations``) instead of writing the same filter again in a normalizer;
- a site **declares** it as data, never as code, per channel or per feed;
- it has an **action**, chosen by the declaration, in the shape Delta Live
  Tables uses for ``expect`` / ``expect_or_drop`` / ``expect_or_fail``.

## The actions, and why the default is to mark

``mark_invalid`` (the default) keeps the row and says what is wrong with it:
``quality = 'bad'`` from ADR-132's closed vocabulary, ``value`` NULL, and the
diagnosis in ``quality_reason`` (``sentinel:655.35``). NULL because ADR-132 D3
says a ``bad`` reading has no value, and because SQL's own ``avg`` then excludes
it: every chart and aggregate is corrected without a reader changing. The number
the instrument sent is not lost. It is in the reason, and bronze still holds the
row.

``drop`` does not write the row. It is never the default: a dropped row is
indistinguishable from a channel that reported nothing, and those are two
different facts. It is counted in the report, never silent.

``fail`` raises :class:`ExpectationFailed`. The deposit lane records that as an
error for the file and moves on; the rows lane stops the pass.

## Where declarations live

``<state_dir>/expectations/*.toml``. Any TOML file there that has ``[[expect]]``
tables is a declaration; everything else in the file is ignored, so a site's
channel map (which already carries ``schema_ref``) can be linked in as-is::

    schema_ref = "site-a/frame-v1"

    [[expect]]
    name = "sentinel"
    channels = ["LOOP:DT*:DewC"]          # fnmatch patterns; omit for every channel
    values = [655.35, 3276.75]            # or: raw_full_scale = 65535, scales = [0.01, 0.05]
    action = "mark_invalid"               # the default; or "drop", or "fail"

A declaration is scoped by ``schema_ref`` or ``site`` (file level or entry
level), and refused without either. One conform process reads every tenant's
bronze, so an unscoped expectation would be one site's instrument knowledge
applied to another site's rows: the same boundary :mod:`..site_rules` keeps for
the frame rules.

## Expectations over a moment

Most expectations judge one row. Some defects are only visible across channels
at one instant: a corrupt acquisition frame can write several related channels
as exactly zero at once while a companion reads a spike, and each of those
values is valid on its own. An expectation with ``evaluates = "moment"`` (such
as ``consistent@1``) is judged once per moment: the rows of one
``(site, feed, ts)``, as they arrived, before any row-level expectation has
nulled a value. When it is not met, its action applies to every row of the
moment (or the channels its ``mark`` names), so the spike goes with the zeros::

    [[expect]]
    name = "consistent"
    label = "companion-zero"                     # quality_reason = "consistent:companion-zero"
    channels = ["TempA", "TempB", "PosA", "PosB"]  # exact names; all must be present
    equals = 0.0                                 # ...and all exactly this value
    action = "mark_invalid"                      # the default; or "drop", or "fail"

A row a row-level expectation already marked keeps that reason: one NULL, one
reason.

Readings arrive one channel per row, so a moment is buffered until the next
moment starts (rows of one bronze record arrive together) or the stage is
flushed. A moment whose channels arrive split, across a flush, a checkpoint or
two conform passes, is judged on what has arrived. A predicate that needs a
channel not yet seen is not met, so nothing is marked on partial evidence; the
moment is counted in ``moments.incomplete``. If the rest of that moment arrives
later in the same pass while it is still remembered (the last
:data:`MOMENT_MEMORY` moments), the predicate is judged on the union and the
late rows are marked; the rows already written are counted in
``moments.written_before_complete`` and corrected by a rebuild, which reads the
moment whole.

## A source that stopped but keeps answering

``stale_source@1`` is a moment expectation with memory. A producer that stamps
``ts`` on receipt, fed by a source that stamps each observation with its own
clock, turns one cached snapshot pushed again on every reconnect into a new
reading per reconnect: same source time, new ``ts``. The first receipt of a
source time is the reading; every later receipt is marked
``stale_source:<label>``::

    [[expect]]
    name = "stale_source"
    observed = "timestamp"                  # the channel with the source's own time
    companions = ["frame.raw"]              # optional: feeds of the same source frame
    action = "mark_invalid"

A companion feed is fanned from the same frame at the same receive ``ts`` but
does not carry the source time, so its moment is marked when the observed
feed's moment at the same (site, ts) is a copy. Bronze is walked a file and a
feed at a time, so a rebuild reads the observed feed once before it writes
(the *prescan*) and judges every companion; an incremental pass judges a
companion only when the observed moment was read first. The memory is per pass
and bounded (:data:`STALE_SOURCE_MEMORY` source times per site and feed).

## Adding an expectation

Write a class with ``check(row) -> Verdict | None`` (or, for a moment,
``check(values) -> Verdict | None`` over ``channel -> value``), describe it with
an :class:`ExpectationSpec` and :func:`register` it. Bump ``version`` when what it
marks changes, so a declaration that pins a version is refused rather than
silently judged by different logic.
"""

from __future__ import annotations

import fnmatch
import logging
import math
import re
import tomllib
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..company import Verdict

log = logging.getLogger(__name__)

#: Directory under the state dir holding declaration files.
EXPECTATIONS_DIR = "expectations"

#: What a declaration may ask for when an expectation is not met.
MARK_INVALID = "mark_invalid"
DROP = "drop"
FAIL = "fail"
ACTIONS = (MARK_INVALID, DROP, FAIL)

#: What an expectation judges: one row, or every row of one (site, feed, ts).
ROW = "row"
MOMENT = "moment"

#: How many recently judged moments are remembered, so one whose channels arrive
#: split within a pass is judged on the union. Bounded: a pass is millions of moments.
MOMENT_MEMORY = 4096


class ExpectationError(ValueError):
    """A declaration that cannot mean what its author thinks it means."""


class ExpectationFailed(RuntimeError):
    """A row failed an expectation declared with ``action = "fail"``."""


@dataclass(frozen=True)
class ExpectationSpec:
    """One catalogue entry: what an expectation is called, does and takes."""

    name: str
    version: int
    description: str
    #: parameter name -> one line saying what it means and its type.
    parameters: dict[str, str]
    #: ``entry -> check``; ``check(row)`` returns a verdict or None. Raises
    #: :class:`ExpectationError` on an entry it cannot honour.
    build: Callable[[Mapping[str, Any]], Callable[[dict[str, Any]], Verdict | None]]
    #: The reason namespace this expectation writes into ``quality_reason``.
    reason_prefix: str = ""
    #: :data:`ROW` (``check(row)``) or :data:`MOMENT` (``check(values)`` over one moment).
    evaluates: str = ROW
    #: The name the platform's utility catalogue (ADR-208) gives this expectation.
    catalog_id: str = ""

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "ref": self.ref,
            "description": self.description,
            "parameters": dict(self.parameters),
            "actions": list(ACTIONS),
            "default_action": MARK_INVALID,
            "quality_reason": f"{self.reason_prefix}<detail>",
            "evaluates": self.evaluates,
            "catalog_id": self.catalog_id or None,
        }


#: Every expectation the platform offers, by name.
CATALOGUE: dict[str, ExpectationSpec] = {}


def register(spec: ExpectationSpec) -> ExpectationSpec:
    """Add ``spec`` to the catalogue. A second spec under one name is refused."""
    held = CATALOGUE.get(spec.name)
    if held is not None and held is not spec:
        raise ValueError(f"expectation {spec.name!r} is already registered ({held.ref})")
    CATALOGUE[spec.name] = spec
    return spec


def catalogue() -> list[dict[str, Any]]:
    """The catalogue as data, sorted by name."""
    return [CATALOGUE[n].describe() for n in sorted(CATALOGUE)]


# --- sentinel ---------------------------------------------------------------


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExpectationError(f"{where}: {value!r} is not a number")
    f = float(value)
    if not math.isfinite(f):
        raise ExpectationError(f"{where}: {value!r} is not finite")
    return f


def sentinel_values(entry: Mapping[str, Any]) -> tuple[float, ...]:
    """The values an entry names, from ``values`` and/or ``raw_full_scale × scales``."""
    out: list[float] = []
    values = entry.get("values")
    if values is not None:
        if not isinstance(values, list) or not values:
            raise ExpectationError("sentinel: `values` must be a non-empty list of numbers")
        out += [_number(v, "sentinel.values") for v in values]
    full = entry.get("raw_full_scale")
    scales = entry.get("scales")
    if full is not None or scales is not None:
        if full is None or not isinstance(scales, list) or not scales:
            raise ExpectationError(
                "sentinel: `raw_full_scale` and `scales` go together — the word the "
                "hardware sends (65535 for 16 bits) and the scale each channel applies"
            )
        raw = _number(full, "sentinel.raw_full_scale")
        out += [raw * _number(s, "sentinel.scales") for s in scales]
    if not out:
        raise ExpectationError(
            "sentinel: names no value — declare `values`, or `raw_full_scale` with `scales`"
        )
    return tuple(sorted(set(out)))


def _matches(value: float, sentinel: float) -> bool:
    # Relative, and tight. A sentinel is an exact word times an exact scale;
    # the tolerance only absorbs the last bit of a decimal scale such as 0.05,
    # so it must never reach a real reading that happens to sit nearby.
    return math.isclose(value, sentinel, rel_tol=1e-9, abs_tol=1e-12)


def _label(v: float) -> str:
    return f"{v:.10g}"


def _build_sentinel(entry: Mapping[str, Any]) -> Callable[[dict[str, Any]], Verdict | None]:
    sentinels = sentinel_values(entry)

    def check(row: dict[str, Any]) -> Verdict | None:
        value = row.get("value")
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        hit = next((s for s in sentinels if _matches(float(value), s)), None)
        if hit is None:
            return None
        channel = str(row.get("channel") or "")
        return Verdict(
            channel,
            "bad",
            f"sentinel:{_label(hit)}",
            f"{channel} = {_label(float(value))} is a declared no-reading value, not a measurement",
            observed=None,
        )

    return check


SENTINEL = register(
    ExpectationSpec(
        name="sentinel",
        version=1,
        description=(
            "A value the acquisition hardware writes when it has no reading — a full-scale "
            "word such as 65535 times the channel's scale (655.35 at 0.01, 3276.75 at 0.05). "
            "Not met when a row's value equals one of the declared values."
        ),
        parameters={
            "values": "list[float] — the exact no-reading values, after scaling",
            "raw_full_scale": "float — the hardware word (e.g. 65535); use with `scales`",
            "scales": "list[float] — scales applied to raw_full_scale (e.g. [0.01, 0.05])",
        },
        build=_build_sentinel,
        reason_prefix="sentinel:",
        catalog_id="core.quality.expect_not_sentinel",
    )
)


# --- consistent -------------------------------------------------------------

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class _ConsistentCheck:
    """``channels`` all exactly ``equals`` at one moment: the moment is not a reading."""

    def __init__(self, label: str, channels: tuple[str, ...], equals: float,
                 mark: tuple[str, ...]) -> None:
        self.label = label
        self.channels = channels
        self.equals = equals
        self.mark = mark

    def _hit(self, value: Any) -> bool:
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return _matches(float(value), self.equals)

    def __call__(self, values: Mapping[str, Any]) -> Verdict | None:
        if not all(c in values and self._hit(values[c]) for c in self.channels):
            return None
        return Verdict(
            "",
            "bad",
            f"consistent:{self.label}",
            f"{', '.join(self.channels)} are all {_label(self.equals)} at once: "
            "the moment is not a reading",
            observed=None,
        )

    def pending(self, values: Mapping[str, Any]) -> bool:
        """Some channels have not arrived and every one that has matches: undecided."""
        present = [c for c in self.channels if c in values]
        return 0 < len(present) < len(self.channels) and all(self._hit(values[c]) for c in present)

    def marks(self, channel: str) -> bool:
        return not self.mark or any(fnmatch.fnmatchcase(channel, p) for p in self.mark)


def _build_consistent(entry: Mapping[str, Any]) -> _ConsistentCheck:
    label = str(entry.get("label") or "")
    if not _LABEL.match(label):
        raise ExpectationError(
            "consistent: `label` names the pattern in quality_reason (consistent:<label>); "
            "letters, digits, '_', '.', '-', at most 64"
        )
    channels = entry.get("channels") or ()
    if isinstance(channels, str) or not isinstance(channels, (list, tuple)) or len(channels) < 2:
        raise ExpectationError("consistent: `channels` must list at least two channel names")
    names = tuple(str(c) for c in channels)
    if any(ch in c for c in names for ch in "*?["):
        raise ExpectationError(
            "consistent: `channels` are exact names, not patterns — 'all of these' must "
            "mean a fixed set"
        )
    if "equals" not in entry:
        raise ExpectationError("consistent: `equals` is required — the value all channels hold at once")
    equals = _number(entry.get("equals"), "consistent.equals")
    mark = entry.get("mark") or ()
    if isinstance(mark, str):
        mark = (mark,)
    if not isinstance(mark, (list, tuple)):
        raise ExpectationError("consistent: `mark` must be a list of channel names or patterns")
    return _ConsistentCheck(label, names, equals, tuple(str(m) for m in mark))


CONSISTENT = register(
    ExpectationSpec(
        name="consistent",
        version=1,
        description=(
            "A cross-channel rule over one moment (site, feed, ts). Not met when every "
            "declared channel holds exactly `equals` at once — for example a corrupt "
            "acquisition frame that writes several related channels as zero while a "
            "companion reads a spike. The action applies to every row of the moment."
        ),
        parameters={
            "label": "str — names the pattern; quality_reason is consistent:<label>",
            "channels": "list[str] — exact channel names; all must be present and match",
            "equals": "float — the value every channel holds at once",
            "mark": "list[str] — optional; which channels of the moment the action applies "
                    "to (names or patterns). Omitted: every channel of the moment",
        },
        build=_build_consistent,
        reason_prefix="consistent:",
        evaluates=MOMENT,
        catalog_id="core.quality.expect_consistent",
    )
)


# --- stale_source -----------------------------------------------------------

#: How many distinct observed times are remembered per (site, feed). A copy
#: repeats within seconds of the last one; a frozen source keeps its one value
#: fresh by repeating it, so a bound this size forgets only originals.
STALE_SOURCE_MEMORY = 65_536


def _instant(ts: Any) -> Any:
    """``ts`` as an aware datetime when it parses, else as it came."""
    from datetime import UTC, datetime

    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        except ValueError:
            return ts
    if isinstance(ts, datetime) and ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


class _StaleSourceCheck:
    """A moment whose source time was already received at an earlier instant is a copy.

    Many sources stamp each observation with their own clock and the receiver
    stamps ``ts`` on arrival. When the source stops producing but keeps
    answering (a cached snapshot pushed again on every reconnect), every copy
    lands as a new reading at a new ``ts`` with the same source time. Each copy
    is a valid-looking reading alone; only the repeat gives it away.

    Stateful over a pass: the first receipt of an observed value is the
    reading, every later receipt is marked. ``companions`` names other feeds
    fanned from the same source frame at the same receive instant, which do
    not carry the source time themselves; a companion moment is marked when the
    observed feed's moment at the same (site, ts) is a copy. A companion can be
    judged only once that moment has been seen, which :meth:`prescan` (run by a
    rebuild before it writes) guarantees.
    """

    def __init__(self, label: str, observed: str, companions: tuple[str, ...],
                 memory: int = STALE_SOURCE_MEMORY) -> None:
        self.label = label
        self.observed = observed
        self.companions = companions
        self.memory = memory
        # (site, feed) -> observed value -> the earliest instant it was received
        self._first: dict[tuple[str, str], OrderedDict[float, Any]] = {}
        # (site, instant) -> (observed value, its first receipt): the copies
        self._copies: dict[tuple[str, Any], tuple[float, Any]] = {}
        self.originals_marked_late = 0

    @staticmethod
    def _value(v: Any) -> float | None:
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        f = float(v)
        return f if math.isfinite(f) else None

    def observe(self, site: str, feed: str, ts: Any, value: Any) -> bool:
        """Record one receipt of the observed time; True when it is a copy."""
        v = self._value(value)
        at = _instant(ts)
        if v is None or isinstance(at, str):
            return False  # nothing to compare: not provably a copy
        key = (site, at)
        seen = self._first.setdefault((site, feed), OrderedDict())
        first = seen.get(v)
        if first is not None:
            seen.move_to_end(v)
        if key in self._copies:
            return True
        if first is None:
            seen[v] = at
            while len(seen) > self.memory:
                seen.popitem(last=False)
            return False
        if first == at:
            return False
        if at < first:
            # Read out of order: this receipt is the original, the one already
            # held is the copy. Written already when this is the main pass.
            self._copies[(site, first)] = (v, at)
            self.originals_marked_late += 1
            seen[v] = at
            return False
        self._copies[key] = (v, first)
        return True

    def prescan(self, row: Mapping[str, Any]) -> None:
        """Observe one row of the observed feed, writing nothing."""
        if str(row.get("channel") or "") == self.observed:
            self.observe(str(row.get("site") or ""), str(row.get("feed") or ""),
                         row.get("ts"), row.get("value"))

    def _verdict(self, v: float, first: Any, *, via: str = "") -> Verdict:
        where = f"the {via} moment at this instant: " if via else ""
        return Verdict(
            "",
            "bad",
            f"stale_source:{self.label}",
            f"{where}source time {self.observed} = {_label(v)} was first received at "
            f"{first.isoformat() if hasattr(first, 'isoformat') else first}; this is a "
            "later copy, so the source did not advance",
            observed=None,
        )

    def judge(self, row: Mapping[str, Any], values: Mapping[str, Any], *,
              companion: bool) -> Verdict | None:
        site = str(row.get("site") or "")
        if companion:
            hit = self._copies.get((site, _instant(row.get("ts"))))
            return self._verdict(*hit, via="observed feed's") if hit is not None else None
        if self.observed not in values:
            return None
        if not self.observe(site, str(row.get("feed") or ""), row.get("ts"), values[self.observed]):
            return None
        v, first = self._copies[(site, _instant(row.get("ts")))]
        return self._verdict(v, first)

    def __call__(self, values: Mapping[str, Any]) -> Verdict | None:
        # Needs the moment's site and instant, which ``values`` does not carry.
        return None

    def covers(self, row: Mapping[str, Any]) -> bool:
        """``row`` belongs to a companion feed of this declaration."""
        return str(row.get("feed") or "") in self.companions


def _build_stale_source(entry: Mapping[str, Any]) -> _StaleSourceCheck:
    observed = entry.get("observed")
    if not isinstance(observed, str) or not observed or any(ch in observed for ch in "*?["):
        raise ExpectationError(
            "stale_source: `observed` names the one channel that carries the source's own "
            "time for the moment (an exact name, not a pattern)"
        )
    label = str(entry.get("label") or observed)
    if not _LABEL.match(label):
        raise ExpectationError(
            "stale_source: `label` names the pattern in quality_reason (stale_source:<label>); "
            "letters, digits, '_', '.', '-', at most 64"
        )
    companions = entry.get("companions") or ()
    if isinstance(companions, str):
        companions = (companions,)
    if not isinstance(companions, (list, tuple)) or any(
        not isinstance(c, str) or not c or any(ch in c for ch in "*?[") for c in companions
    ):
        raise ExpectationError("stale_source: `companions` must list exact feed names")
    if entry.get("channels"):
        raise ExpectationError(
            "stale_source: judges the whole moment and every row of it; name the source-time "
            "channel in `observed`, not `channels`"
        )
    return _StaleSourceCheck(label, observed, tuple(companions))


STALE_SOURCE = register(
    ExpectationSpec(
        name="stale_source",
        version=1,
        description=(
            "A source that stamps each observation with its own time, received again later. "
            "Not met when a moment's `observed` source time equals one already received at an "
            "earlier ts for the same site and feed: a replayed or cached copy, not a new "
            "reading. The first receipt stays. The action applies to every row of the "
            "moment, and to the moments of `companions` at the same site and ts."
        ),
        parameters={
            "observed": "str — the channel carrying the source's own time for the moment",
            "label": "str — optional; quality_reason is stale_source:<label> (default: observed)",
            "companions": "list[str] — optional; feeds fanned from the same source frame at the "
                          "same receive ts that do not carry the source time. Judged fully by a "
                          "rebuild, which reads the observed feed first",
        },
        build=_build_stale_source,
        reason_prefix="stale_source:",
        evaluates=MOMENT,
        catalog_id="core.quality.expect_source_advances",
    )
)


# --- declarations -----------------------------------------------------------


@dataclass(frozen=True)
class Declared:
    """One ``[[expect]]`` entry, resolved against the catalogue."""

    spec: ExpectationSpec
    action: str
    check: Callable[[dict[str, Any]], Verdict | None]
    schema_ref: str = ""
    site: str = ""
    feed: str = ""
    channels: tuple[str, ...] = ()
    source: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def per_moment(self) -> bool:
        return self.spec.evaluates == MOMENT

    def in_scope(self, row: Mapping[str, Any]) -> bool:
        """The row's schema, site and feed are this declaration's (its channel aside)."""
        if self.schema_ref and str(row.get("schema_ref") or "") != self.schema_ref:
            return False
        if self.site and str(row.get("site") or "") != self.site:
            return False
        if self.feed and str(row.get("feed") or "") != self.feed:
            return False
        return True

    def companion_of(self, row: Mapping[str, Any]) -> bool:
        """``row`` is a companion feed's (another schema), same site scope."""
        covers = getattr(self.check, "covers", None)
        if covers is None or not covers(row):
            return False
        return not self.site or str(row.get("site") or "") == self.site

    @property
    def prescans(self) -> bool:
        """Judging this needs the observed feed seen before anything is written."""
        return self.per_moment and bool(getattr(self.check, "companions", ()))

    def prescan(self, row: Mapping[str, Any]) -> None:
        if self.in_scope(row):
            self.check.prescan(row)  # type: ignore[attr-defined]

    def applies_to(self, row: Mapping[str, Any]) -> bool:
        if not self.in_scope(row):
            return False
        if self.channels and not self.per_moment:
            channel = str(row.get("channel") or "")
            return any(fnmatch.fnmatchcase(channel, p) for p in self.channels)
        return True

    def describe(self) -> dict[str, Any]:
        return {
            "expectation": self.spec.ref,
            "action": self.action,
            "schema_ref": self.schema_ref or None,
            "site": self.site or None,
            "feed": self.feed or None,
            "channels": list(self.channels) or None,
            "params": dict(self.params),
            "source": self.source,
        }


_SCOPE_KEYS = ("name", "version", "action", "schema_ref", "site", "feed", "channels")


def declared_from(doc: Mapping[str, Any], *, source: str = "") -> list[Declared]:
    """The expectations one parsed document declares. ``[]`` when it has none.

    Refuses rather than skips. A declaration somebody wrote and nothing honours
    reads as a check being made when it is not.
    """
    entries = doc.get("expect")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ExpectationError(f"{source}: `expect` must be an array of tables ([[expect]])")
    file_ref = str(doc.get("schema_ref") or "")
    file_site = str(doc.get("site") or "")
    out: list[Declared] = []
    for i, entry in enumerate(entries):
        where = f"{source}: expect[{i}]"
        if not isinstance(entry, Mapping):
            raise ExpectationError(f"{where} must be a table")
        name = str(entry.get("name") or "")
        spec = CATALOGUE.get(name)
        if spec is None:
            raise ExpectationError(
                f"{where}: unknown expectation {name!r}; the catalogue has {sorted(CATALOGUE)}"
            )
        pinned = entry.get("version")
        if pinned is not None and int(pinned) != spec.version:
            raise ExpectationError(
                f"{where}: pins {name}@{pinned}, the platform has {spec.ref}. Refused rather "
                "than judged by logic the declaration did not ask for."
            )
        action = str(entry.get("action") or MARK_INVALID)
        if action not in ACTIONS:
            raise ExpectationError(f"{where}: action {action!r} is not one of {list(ACTIONS)}")
        schema_ref = str(entry.get("schema_ref") or file_ref)
        site = str(entry.get("site") or file_site)
        if not schema_ref and not site:
            raise ExpectationError(
                f"{where}: scoped to nothing. Declare `schema_ref` or `site` (in the entry or "
                "at the top of the file): one conform pass reads every tenant's rows, and an "
                "unscoped expectation would judge all of them."
            )
        channels = entry.get("channels") or ()
        if isinstance(channels, str):
            channels = (channels,)
        if not isinstance(channels, (list, tuple)):
            raise ExpectationError(f"{where}: `channels` must be a list of names or patterns")
        params = {k: v for k, v in entry.items() if k not in _SCOPE_KEYS}
        try:
            # A moment expectation's channels are its predicate, not a row filter.
            check = spec.build({**params, "channels": list(channels)} if spec.evaluates == MOMENT else params)
        except ExpectationError as exc:
            raise ExpectationError(f"{where}: {exc}") from exc
        out.append(
            Declared(
                spec=spec,
                action=action,
                check=check,
                schema_ref=schema_ref,
                site=site,
                feed=str(entry.get("feed") or ""),
                channels=tuple(str(c) for c in channels),
                source=source,
                params=params,
            )
        )
    return out


def declarations_dir(state_dir: Path | str | None) -> Path:
    root = Path(state_dir).expanduser() if state_dir else Path("~/.axi").expanduser()
    return root / EXPECTATIONS_DIR


def load_declared(state_dir: Path | str | None) -> tuple[list[Declared], dict[str, str]]:
    """Every expectation declared under ``<state_dir>/expectations/``.

    Returns ``(declared, unreadable)``. Symlinks are followed, so a deploy can
    link a site repo's file in place. A file that cannot be read or does not
    mean anything is reported in ``unreadable`` by path and contributes
    nothing; the other files still load. One site's typo must not stop every
    other site's expectations, and it must not pass quietly either.
    """
    root = declarations_dir(state_dir)
    try:
        paths = sorted(p for p in root.iterdir() if p.suffix == ".toml")
    except OSError:
        return [], {}
    out: list[Declared] = []
    unreadable: dict[str, str] = {}
    for path in paths:
        try:
            doc = tomllib.loads(path.read_text(encoding="utf-8"))
            out += declared_from(doc, source=str(path))
        except (OSError, tomllib.TOMLDecodeError, ExpectationError, ValueError) as exc:
            unreadable[str(path)] = str(exc)
            log.warning("expectations: %s refused: %s", path, exc)
    return out, unreadable


# --- the conform stage ------------------------------------------------------


def expecting_upsert(inner: Callable[[dict[str, Any]], None], declared: list[Declared]):
    """Wrap an upsert so every row meets the declared expectations first.

    Returns ``(upsert, report)``. The upsert always has a ``flush()``, which the
    caller must call at the end of a run and before a checkpoint: a moment
    expectation holds the current moment until the next one starts. With no
    moment expectation declared nothing is held and every row goes straight
    through. ``flush`` forwards to ``inner.flush`` when there is one, so this can
    sit outside the buffering VALIDATE stage. It runs BEFORE that stage on
    purpose: a sentinel nulled here is a reading the frame rules then see as
    absent, rather than a 655.35 they would compare against its neighbours.

    Per row, the first verdict wins and the rest are not run: a row is either a
    reading or it is not, and two reasons for one NULL would make the count
    ambiguous. A moment verdict is decided on the values as they arrived, then
    applies to each row of the moment that no row-level expectation marked.
    """
    report: dict[str, Any] = {
        "declared": [d.describe() for d in declared],
        "checked": 0,
        "marked": 0,
        "dropped": 0,
        "failed": 0,
        "reasons": {},
        "by_expectation": {},
    }
    per_row = [d for d in declared if not d.per_moment]
    per_moment = [d for d in declared if d.per_moment]

    def _count(d: Declared, verdict: Verdict) -> None:
        per = report["by_expectation"].setdefault(d.spec.ref, {MARK_INVALID: 0, DROP: 0, FAIL: 0})
        per[d.action] += 1
        report["reasons"][verdict.reason] = report["reasons"].get(verdict.reason, 0) + 1

    def _apply(row: dict[str, Any], d: Declared, verdict: Verdict) -> dict[str, Any] | None:
        """The row after ``d``'s action; None when it is dropped. Raises on ``fail``."""
        _count(d, verdict)
        if d.action == FAIL:
            report["failed"] += 1
            raise ExpectationFailed(
                f"{d.spec.ref} not met ({verdict.because}); declared action is fail ({d.source})"
            )
        if d.action == DROP:
            report["dropped"] += 1
            return None
        row = dict(row)
        row["quality"] = verdict.quality
        row["quality_reason"] = verdict.reason
        # ADR-132 D3: a `bad` reading carries no value.
        row["value"] = None
        report["marked"] += 1
        return row

    def _row_stage(row: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """``(row or None if dropped, whether a row-level expectation judged it)``."""
        report["checked"] += 1
        for d in per_row:
            if not d.applies_to(row):
                continue
            verdict = d.check(row)
            if verdict is None:
                continue
            return _apply(row, d, verdict), True
        return row, False

    if not per_moment:
        def _do(row: dict[str, Any]) -> None:
            out, _ = _row_stage(row)
            if out is not None:
                inner(out)
    else:
        moments = report["moments"] = {
            "judged": 0, "matched": 0, "incomplete": 0, "reopened": 0,
            "completed_late": 0, "written_before_complete": 0,
        }
        held: list[dict[str, Any]] = []
        key: tuple[Any, ...] | None = None
        # Recently judged moments: what they carried, and whether any was undecided.
        recent: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()

        def _emit() -> None:
            nonlocal held, key
            if not held:
                return
            rows, this = held, key
            held, key = [], None
            values = {str(r.get("channel") or ""): r.get("value") for r in rows}
            earlier = recent.pop(this, None)
            if earlier is not None:
                moments["reopened"] += 1
                values = {**earlier["values"], **values}
            moments["judged"] += 1
            hit: tuple[Declared, Verdict] | None = None
            undecided = False
            for d in per_moment:
                companion = False
                if not d.in_scope(rows[0]):
                    if not d.companion_of(rows[0]):
                        continue
                    companion = True
                judge = getattr(d.check, "judge", None)
                if judge is not None:
                    # Needs the moment's site, feed and instant, not only its values.
                    verdict = judge(rows[0], values, companion=companion)
                elif companion:
                    continue
                else:
                    verdict = d.check(values)
                if verdict is not None:
                    hit = (d, verdict)
                    break
                pending = getattr(d.check, "pending", None)
                undecided = undecided or bool(pending is not None and pending(values))
            if hit is not None:
                moments["matched"] += 1
                if earlier is not None and earlier["undecided"]:
                    moments["completed_late"] += 1
                    moments["written_before_complete"] += earlier["rows"]
            elif undecided:
                moments["incomplete"] += 1
            recent[this] = {"values": values, "undecided": hit is None and undecided,
                            "rows": len(rows) + (earlier["rows"] if earlier else 0)}
            while len(recent) > MOMENT_MEMORY:
                recent.popitem(last=False)
            for row in rows:
                out, judged = _row_stage(row)
                if out is None:
                    continue
                marks = getattr(hit[0].check, "marks", None) if hit is not None else None
                if hit is not None and not judged and (marks is None or marks(str(row.get("channel") or ""))):
                    out = _apply(out, hit[0], hit[1])
                    if out is None:
                        continue
                inner(out)

        def _do(row: dict[str, Any]) -> None:
            nonlocal key
            this = (row.get("site"), row.get("feed"), row.get("ts"))
            if key is not None and this != key:
                _emit()
            key = this
            held.append(row)

    def _flush() -> None:
        if per_moment:
            _emit()
            # An original read after its copy was written: that copy is in
            # silver as a reading until a rebuild, which reads in order.
            late = sum(int(getattr(d.check, "originals_marked_late", 0)) for d in per_moment)
            if late:
                moments["originals_read_late"] = late
        flush = getattr(inner, "flush", None)
        if flush is not None:
            flush()

    _do.flush = _flush  # type: ignore[attr-defined]
    return _do, report


__all__ = [
    "ACTIONS",
    "CATALOGUE",
    "CONSISTENT",
    "MOMENT",
    "MOMENT_MEMORY",
    "ROW",
    "DROP",
    "EXPECTATIONS_DIR",
    "FAIL",
    "MARK_INVALID",
    "SENTINEL",
    "STALE_SOURCE",
    "STALE_SOURCE_MEMORY",
    "Declared",
    "ExpectationError",
    "ExpectationFailed",
    "ExpectationSpec",
    "catalogue",
    "declarations_dir",
    "declared_from",
    "expecting_upsert",
    "load_declared",
    "register",
    "sentinel_values",
]
