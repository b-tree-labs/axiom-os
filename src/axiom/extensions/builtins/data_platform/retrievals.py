# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A named retrieval: what to fetch, said once, reusable on every surface.

## The problem, found by walking a real node

The same idea is spelled differently on every surface. The chart API takes
`t_from`/`t_to`; the served telemetry API takes `start`/`end`. Neither rejects
the other's spelling — FastAPI ignores query parameters it does not declare —
so a caller who brings the wrong one gets **a confident answer to a different
question**. Asking for September and receiving the whole record looks exactly
like asking for September.

Retyping a retrieval per surface is where that mistake lives. Naming it once,
and letting each surface ask this module for its own dialect, removes the
retyping and therefore the mistake.

## What a retrieval is, and what it deliberately is not

It is a **question**, not an answer: site, feed, channels, and optionally a
window. It holds no data and no results. Two people with the same retrieval
name are asking the same question of whatever the record says today.

It is not a dashboard and not a chart. A chart is one way to *draw* the answer;
the same retrieval feeds a CSV, an agent, and a figure. Keeping them separate is
what lets a retrieval composed in chat be opened in a chart, and a chart's
current view be saved as a retrieval.

## Three decisions worth arguing with

**The window is a SPAN by default, not two instants.** A saved retrieval named
`last-day` holding `2026-09-10T08:00→15:30` is a historical artifact the first
time tomorrow arrives, and its name has become a lie. A span stays true. Two
instants are still allowed, because sometimes one particular run *is* the
question — and then `frozen` says so out loud, since a reader who expects
"recent" from a name needs to know it means one fixed day forever.

**The window may be left OPEN.** It is the part a caller varies most, so a
retrieval that names only WHAT to fetch lets one name serve every day. This is
the difference between a catalog of five useful questions and a catalog of five
hundred date-stamped ones.

**A field this version does not understand survives a round trip.** Two
versions of the tool will share one catalog file the moment anybody upgrades,
and a loader that drops an unknown key is worse than one that never stored it:
the writer believes it was saved. So the raw mapping is kept alongside the
typed view and written back intact.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: A name that works as a CLI argument, a URL segment and a catalog key without
#: quoting or escaping in any of the three.
NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

#: Two instants, `from/to`. The presence of the slash is what makes a window
#: frozen — a span never contains one.
FROZEN_SEP = "/"

#: The surfaces this translates for. Each spells the window differently and
#: silently ignores the others' spelling, which is the whole reason this exists.
DIALECTS = ("chart", "telemetry")


class NameTaken(ValueError):
    """A retrieval by that name is already in the catalog."""


@dataclass(frozen=True)
class Retrieval:
    """A named question about data. Validated on construction, never later.

    Validating here rather than at save time means an invalid retrieval cannot
    be held in memory, passed around, and rejected only when somebody tries to
    store it — by which point the caller has moved on from the mistake.
    """

    name: str
    site: str = ""
    feed: str = ""
    channels: tuple[str, ...] = ()
    #: A span (`24h`, `all`, `operating`), two instants (`from/to`), or empty
    #: for "the caller says when".
    window: str = ""
    bucket: str = ""
    note: str = ""
    #: Everything a newer version wrote that this one does not model. Kept so a
    #: round trip through an older build does not silently delete it.
    extra: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not NAME_OK.match(self.name or ""):
            raise ValueError(
                f"retrieval name {self.name!r} must be lowercase letters, digits, "
                "dot, dash or underscore — it has to work as a CLI argument and a "
                "URL segment without quoting"
            )
        if not self.channels:
            raise ValueError(
                f"retrieval {self.name!r} names no channel, so it names no data; "
                "a name for nothing fails at call time instead of here"
            )

    @property
    def frozen(self) -> bool:
        """True when the window is two instants, so it means one fixed period
        forever however the name reads."""
        return FROZEN_SEP in self.window

    @property
    def open_window(self) -> bool:
        """True when the retrieval names what to fetch and leaves when to the
        caller."""
        return not self.window

    def as_dict(self) -> dict[str, Any]:
        """The stored shape: unknown fields first so known ones win on collision."""
        out = dict(self.extra)
        out.update(
            {
                "name": self.name,
                "site": self.site,
                "feed": self.feed,
                "channels": list(self.channels),
                "window": self.window,
                "bucket": self.bucket,
                "note": self.note,
            }
        )
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Retrieval:
        known = {"name", "site", "feed", "channels", "window", "bucket", "note"}
        return cls(
            name=str(raw.get("name", "")),
            site=str(raw.get("site", "") or ""),
            feed=str(raw.get("feed", "") or ""),
            channels=tuple(raw.get("channels") or ()),
            window=str(raw.get("window", "") or ""),
            bucket=str(raw.get("bucket", "") or ""),
            note=str(raw.get("note", "") or ""),
            extra={k: v for k, v in raw.items() if k not in known},
        )


def as_query(r: Retrieval, *, dialect: str) -> dict[str, str]:
    """`r` as the query parameters `dialect` actually reads.

    Refuses an unknown dialect rather than returning a best guess: a caller
    handed parameters a surface ignores is the exact failure this module was
    written to end, and returning something plausible would reproduce it one
    layer up.
    """
    if dialect not in DIALECTS:
        raise ValueError(f"unknown dialect {dialect!r}; this translates for {DIALECTS}")

    q: dict[str, str] = {}
    if r.feed:
        q["feed"] = r.feed
    if r.channels:
        q["channels"] = ",".join(r.channels)
    if r.bucket:
        q["bucket"] = r.bucket

    if not r.window:
        return q
    if r.frozen:
        began, _, ended = r.window.partition(FROZEN_SEP)
        # The two spellings this module exists because of.
        keys = ("t_from", "t_to") if dialect == "chart" else ("start", "end")
        q[keys[0]], q[keys[1]] = began, ended
        return q
    # A span. The chart resolves `last` itself against where its readings end;
    # the telemetry API has no span parameter at all, so a span is reported as
    # the caller's to resolve rather than silently dropped.
    if dialect == "chart":
        q["last"] = r.window
    else:
        q["_span"] = r.window
    return q


class Catalog:
    """The local file of named retrievals.

    Local on purpose. A retrieval is somebody's saved question, and making the
    first version a shared service would mean nobody can name one until an
    account exists. Sharing is a later, deliberate step — the file is the thing
    that makes it possible, not a thing that prevents it.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _raw(self) -> list[dict[str, Any]]:
        try:
            loaded = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            # A missing catalog is an empty one. Somebody who has never saved a
            # retrieval is in a normal state, not an error state.
            return []
        items = loaded.get("retrievals") if isinstance(loaded, dict) else loaded
        return [i for i in (items or []) if isinstance(i, dict)]

    def list(self) -> list[Retrieval]:
        out = []
        for raw in self._raw():
            try:
                out.append(Retrieval.from_dict(raw))
            except ValueError:
                # One unreadable entry must not hide the rest. A catalog that
                # raises on the whole file because of one bad row is a catalog
                # somebody deletes.
                continue
        return sorted(out, key=lambda r: r.name)

    def get(self, name: str) -> Retrieval:
        for r in self.list():
            if r.name == name:
                return r
        have = ", ".join(x.name for x in self.list()) or "(none saved)"
        raise KeyError(f"no retrieval named {name!r}; the catalog holds: {have}")

    def save(self, r: Retrieval, *, replace: bool = False) -> None:
        rows = self._raw()
        at = next((i for i, row in enumerate(rows) if row.get("name") == r.name), None)
        if at is not None and not replace:
            raise NameTaken(
                f"a retrieval named {r.name!r} already exists; pass replace to overwrite it"
            )
        if at is None:
            rows.append(r.as_dict())
        else:
            # Merge over the stored row so a newer version's fields survive an
            # edit made by this one.
            merged = dict(rows[at])
            merged.update(r.as_dict())
            rows[at] = merged
        self._write(rows)

    def remove(self, name: str) -> bool:
        rows = self._raw()
        keep = [row for row in rows if row.get("name") != name]
        if len(keep) == len(rows):
            return False
        self._write(keep)
        return True

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"retrievals": rows}, indent=2, sort_keys=True) + "\n")
        # Replace, so an interrupted write cannot leave a half-file where the
        # catalog was. Losing a saved question to a crash is not recoverable by
        # the person who saved it.
        tmp.replace(self.path)


__all__ = ["DIALECTS", "NAME_OK", "Catalog", "NameTaken", "Retrieval", "as_query"]
