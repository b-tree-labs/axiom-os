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

## Adding an expectation

Write a class with ``check(row) -> Verdict | None``, describe it with an
:class:`ExpectationSpec` and :func:`register` it. Bump ``version`` when what it
marks changes, so a declaration that pins a version is refused rather than
silently judged by different logic.
"""

from __future__ import annotations

import fnmatch
import logging
import math
import tomllib
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

    def applies_to(self, row: Mapping[str, Any]) -> bool:
        if self.schema_ref and str(row.get("schema_ref") or "") != self.schema_ref:
            return False
        if self.site and str(row.get("site") or "") != self.site:
            return False
        if self.feed and str(row.get("feed") or "") != self.feed:
            return False
        if self.channels:
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
            check = spec.build(params)
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

    Returns ``(upsert, report)``. Per row, holds nothing, so it needs no flush of
    its own; it forwards ``inner.flush`` when there is one, so it can sit outside
    the buffering VALIDATE stage. It runs BEFORE that stage on purpose: a
    sentinel nulled here is a reading the frame rules then see as absent, rather
    than a 655.35 they would compare against its neighbours.

    The first verdict wins and the rest are not run: a row is either a reading or
    it is not, and two reasons for one NULL would make the count ambiguous.
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

    def _do(row: dict[str, Any]) -> None:
        report["checked"] += 1
        for d in declared:
            if not d.applies_to(row):
                continue
            verdict = d.check(row)
            if verdict is None:
                continue
            ref = d.spec.ref
            per = report["by_expectation"].setdefault(ref, {MARK_INVALID: 0, DROP: 0, FAIL: 0})
            per[d.action] += 1
            report["reasons"][verdict.reason] = report["reasons"].get(verdict.reason, 0) + 1
            if d.action == FAIL:
                report["failed"] += 1
                raise ExpectationFailed(
                    f"{ref} not met ({verdict.because}); declared action is fail ({d.source})"
                )
            if d.action == DROP:
                report["dropped"] += 1
                return
            row = dict(row)
            row["quality"] = verdict.quality
            row["quality_reason"] = verdict.reason
            # ADR-132 D3: a `bad` reading carries no value.
            row["value"] = None
            report["marked"] += 1
            break
        inner(row)

    flush = getattr(inner, "flush", None)
    if flush is not None:
        _do.flush = flush  # type: ignore[attr-defined]
    return _do, report


__all__ = [
    "ACTIONS",
    "CATALOGUE",
    "DROP",
    "EXPECTATIONS_DIR",
    "FAIL",
    "MARK_INVALID",
    "SENTINEL",
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
