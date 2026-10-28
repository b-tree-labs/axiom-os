# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A figure source whose readings come from a node's data door.

The Charts surface asks a figure lane for five things: the sites, a site's
channels, the days a set of channels has readings on, a revision to cache by,
and a figure of some channels over a window. This module answers all five from
a remote node, through ``POST /data/query`` with a bearer key, and draws the
figure with the platform's own renderer (:func:`chart_svg.render_svg`). What it
draws is therefore what the node would draw from the same rows.

It reads only the served gold skills (``data.aggregate`` over
``gold.signal_catalogue`` for the catalogue, ``data.series`` over
``gold.signals`` for readings). Silver never serves, and nothing here can ask
for it: the door holds each verb to gold and to the sites on the caller's key.

The key is resolved from a vault reference on every cold start and held in
memory only. It is never logged, printed or written.

Cost is bounded on this side as well as the node's (ADR-157):

* the bucket is chosen so a series is about ``target_points`` points;
* a wide window is read as at most ``max_chunks`` concurrent reads rather than
  one wide scan, paced under the door's interactive rate limit;
* a window that would scan more than ``row_budget`` catalogue rows is clamped
  to its most recent part, and the figure says so in ``notes``.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .chart_svg import Geometry, Series, render_svg

#: Bucket ladder, seconds. The chosen bucket is the first rung at or above the
#: span divided by the point target, so a day is two-minute means, a week
#: fifteen-minute means.
_LADDER = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200,
           10800, 21600, 43200, 86400)

CATALOGUE_TABLE = "signal_catalogue"
SIGNALS_TABLE = "signals"


class NodeReadError(RuntimeError):
    """The node did not answer a read, or answered it with an error."""


def _interval(seconds: int) -> str:
    if seconds % 86400 == 0:
        n, unit = seconds // 86400, "day"
    elif seconds % 3600 == 0:
        n, unit = seconds // 3600, "hour"
    elif seconds % 60 == 0:
        n, unit = seconds // 60, "minute"
    else:
        n, unit = seconds, "second"
    return f"{n} {unit}" + ("s" if n != 1 else "")


def choose_bucket(span_seconds: float, target_points: int = 900) -> int:
    """The bucket, in seconds, that draws ``span_seconds`` in about ``target_points``."""
    want = max(1.0, span_seconds / max(1, target_points))
    return next((b for b in _LADDER if b >= want), _LADDER[-1])


def _parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).replace("Z", "+00:00").replace(" ", "T", 1)
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _rows_per_day(c: Channel) -> float:
    if not (c.first and c.last and c.rows):
        return 0.0
    days = max(1.0, (c.last - c.first).total_seconds() / 86400)
    return c.rows / days


def _span_words(span: timedelta) -> str:
    hours = span.total_seconds() / 3600
    if hours < 48:
        return f"{hours:.0f} hours"
    return f"{hours / 24:.0f} days"


def key_from_vault(
    ref: str, *, openbao_url: str = "", token_file: str = "", mount: str = "kv",
) -> str:
    """The bearer key a vault reference names.

    The platform resolver is tried first (it honours the vault settings file and
    environment). When that has no OpenBao token configured, an explicit
    ``openbao_url`` and ``token_file`` are used instead. The token file holds
    the vault token, never the key.
    """
    from axiom.extensions.builtins.secrets import SecretRef, resolve

    parsed = SecretRef.parse(ref if "://" in ref else f"openbao://{ref}")
    value: Any = None
    try:
        value = resolve(parsed).value
    except Exception:  # noqa: BLE001 — fall through to the explicit vault below
        if not (openbao_url and token_file):
            raise
    if value is None:
        from axiom.extensions.builtins.secrets.providers.openbao import (
            OpenBaoSecretStoreProvider,
        )

        token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        store = OpenBaoSecretStoreProvider(
            {"url": openbao_url, "token": token, "mount": mount, "uid": "figure-source"}
        ).open()
        # A bare name lives under the kv/v2 mount. Spelled out in full, so a
        # provider that takes paths as given reads the same secret.
        path = parsed.path.strip("/")
        if not path.startswith(f"{mount}/"):
            parsed = SecretRef.parse(f"{parsed.scheme}://{mount}/data/{path}")
        value = store.get(parsed).value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    key = str(value or "").strip()
    if not key:
        raise NodeReadError(f"the vault reference {ref!r} holds no key")
    return key


class _Pace:
    """A token bucket: at most ``rate`` reads a second, ``burst`` at once.

    The door holds interactive callers to a rate; a client that knows the rate
    waits its turn instead of being refused.
    """

    def __init__(self, rate: float, burst: int) -> None:
        self.rate, self.burst = rate, float(burst)
        self._tokens, self._at = float(burst), time.monotonic()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
                self._at = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                need = (1 - self._tokens) / self.rate
            time.sleep(need)


@dataclass
class DataDoor:
    """``POST {node}/data/query`` with a bearer key. Reads only."""

    node_url: str
    key: Callable[[], str]
    timeout: float = 30.0
    #: Reads a second and burst, kept under the door's own interactive limit.
    rate: float = 4.0
    burst: int = 12
    retries: int = 3
    _cached_key: str = field(default="", repr=False)
    _pace: _Pace | None = field(default=None, repr=False)

    def _bearer(self) -> str:
        if not self._cached_key:
            self._cached_key = self.key()
        return self._cached_key

    def query(self, verb: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if self._pace is None:
            self._pace = _Pace(self.rate, self.burst)
        for attempt in range(self.retries + 1):
            self._pace.wait()
            try:
                return self._query_once(verb, params)
            except NodeReadError as exc:
                if "rate limit" not in str(exc) or attempt == self.retries:
                    raise
                time.sleep(0.5 * (attempt + 1))
        raise NodeReadError(f"{verb}: not answered")  # pragma: no cover — loop always returns or raises

    def _query_once(self, verb: str, params: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps({"verb": verb, "params": dict(params)}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.node_url.rstrip('/')}/data/query",
            data=body,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self._bearer()}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                err = json.loads(exc.read().decode("utf-8"))
                detail = str((err.get("error") or {}).get("message") or err.get("detail") or "")
            except Exception:  # noqa: BLE001 — a body that is not JSON keeps the code
                pass
            raise NodeReadError(
                f"{verb}: the node answered HTTP {exc.code}" + (f": {detail}" if detail else "")
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NodeReadError(f"{verb}: the node could not be reached ({reason})") from None
        if not isinstance(payload, dict):
            raise NodeReadError(f"{verb}: the node returned no result")
        errors = [str(e) for e in payload.get("errors") or []]
        if errors or payload.get("ok") is False:
            raise NodeReadError(f"{verb}: " + "; ".join(errors or ["refused"]))
        value = payload.get("value")
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Channel:
    site: str
    feed: str
    channel: str
    unit: str
    rows: int
    first: datetime | None
    last: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "feed": self.feed,
            "channel": self.channel,
            "unit": self.unit,
            "rows": self.rows,
            "first": _iso(self.first) if self.first else None,
            "last": _iso(self.last) if self.last else None,
        }


class FigureNote(ValueError):
    """What was asked for is not there. The surface shows it as a note, not a fault."""


@dataclass
class NodeFigureSource:
    """The five answers a Charts surface needs, from a node's gold."""

    door: DataDoor
    labels: Mapping[str, str] = field(default_factory=dict)
    catalogue_ttl: float = 600.0
    #: Seconds a channel's day calendar is fresh. Older is served, then re-read.
    dates_ttl: float = 6 * 3600.0
    #: Where day calendars persist across restarts. Empty keeps them in memory only.
    dates_cache_path: str = ""
    target_points: int = 900
    #: The smallest read a wide window is split into.
    chunk: timedelta = timedelta(days=1)
    #: At most this many reads for one figure.
    max_chunks: int = 8
    #: Catalogue rows one figure may scan, across its channels. A window that
    #: would scan more is clamped to its most recent part, and says so.
    row_budget: int = 400_000
    #: Catalogue rows one read of the day calendar may scan.
    dates_chunk_rows: int = 400_000
    #: Never clamp a figure below this, however dense the channels.
    min_window: timedelta = timedelta(days=1)
    workers: int = 4
    _catalogue: tuple[float, dict[str, list[Channel]]] | None = field(default=None, repr=False)
    _dates: dict[str, tuple[float, dict[str, int]]] = field(default_factory=dict, repr=False)
    _inflight: dict[str, Any] = field(default_factory=dict, repr=False)
    _background: ThreadPoolExecutor | None = field(default=None, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # --- the catalogue --------------------------------------------------------
    def catalogue(self, *, refresh: bool = False) -> dict[str, list[Channel]]:
        with self._lock:
            cached = self._catalogue
        if cached and not refresh and time.monotonic() - cached[0] < self.catalogue_ttl:
            return cached[1]
        group = ["site", "feed", "channel", "unit"]
        asks = {"rows": "sum", "first_ts": "min", "last_ts": "max"}

        def one(column: str, fn: str) -> list[dict[str, Any]]:
            value = self.door.query("data.aggregate", {
                "table": CATALOGUE_TABLE, "column": column, "fn": fn,
                "group_by": group, "allow_mixed_units": True,
            })
            return list((value.get("data") or {}).get("groups") or [])

        with ThreadPoolExecutor(max_workers=3) as pool:
            answers = dict(zip(asks, pool.map(lambda kv: one(*kv), asks.items())))
        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        for column, groups in answers.items():
            for g in groups:
                k = (str(g.get("site") or ""), str(g.get("feed") or ""), str(g.get("channel") or ""))
                slot = merged.setdefault(k, {"unit": g.get("unit") or ""})
                slot["unit"] = slot["unit"] or (g.get("unit") or "")
                slot[column] = g.get("value")
        out: dict[str, list[Channel]] = {}
        for (site, feed, channel), v in sorted(merged.items()):
            if not site or not channel:
                continue
            try:
                rows = int(float(v.get("rows") or 0))
            except (TypeError, ValueError):
                rows = 0
            out.setdefault(site, []).append(Channel(
                site=site, feed=feed, channel=channel, unit=str(v.get("unit") or ""),
                rows=rows, first=_parse_ts(v.get("first_ts")), last=_parse_ts(v.get("last_ts")),
            ))
        with self._lock:
            self._catalogue = (time.monotonic(), out)
        return out

    def sites(self) -> list[dict[str, Any]]:
        cat = self.catalogue()
        return [
            {"id": site, "label": self.labels.get(site, site), "rows": sum(c.rows for c in chans)}
            for site, chans in sorted(cat.items())
        ]

    def channels(self, site: str) -> list[dict[str, Any]]:
        return [c.as_dict() for c in self.catalogue().get(site, [])]

    def revision(self, site: str) -> str:
        """Changes when the site's newest reading does, so a cache keyed by it stays honest."""
        last = max((c.last for c in self.catalogue().get(site, []) if c.last), default=None)
        return str(int(last.timestamp())) if last else ""

    def resolve(self, site: str, tokens: Iterable[str], feed: str = "") -> list[Channel]:
        """Requested channels to catalogue entries.

        A token is ``channel``, ``feed:channel`` or ``feed|channel``. A channel
        name may itself hold colons (EPICS process variables do), so a prefix
        counts as a feed only when the site has that feed.
        """
        chans = self.catalogue().get(site, [])
        feeds = {c.feed for c in chans}
        by_key = {(c.feed, c.channel): c for c in chans}
        found: list[Channel] = []
        for raw in tokens:
            t = raw.strip()
            if not t:
                continue
            f, ch = "", t
            for sep in ("|", ":"):
                head, _, tail = t.partition(sep)
                if tail and head in feeds:
                    f, ch = head, tail
                    break
            f = f or feed
            hit = by_key.get((f, ch)) if f else None
            if hit is None:
                hit = next((c for c in chans if c.channel == ch and (not f or c.feed == f)), None)
            if hit is None:
                hit = next((c for c in chans if c.channel == ch), None)
            if hit is not None and hit not in found:
                found.append(hit)
        return found

    # --- reads ----------------------------------------------------------------
    def _filter(self, site: str, chans: list[Channel]) -> str:
        feeds = sorted({c.feed for c in chans})
        names = sorted({c.channel for c in chans})
        parts = [f"site = {_quote(site)}"]
        parts.append(f"feed = {_quote(feeds[0])}" if len(feeds) == 1
                     else "feed IN (" + ", ".join(map(_quote, feeds)) + ")")
        parts.append(f"channel = {_quote(names[0])}" if len(names) == 1
                     else "channel IN (" + ", ".join(map(_quote, names)) + ")")
        return " AND ".join(parts)

    def _series_chunk(self, site: str, chans: list[Channel], start: datetime, end: datetime,
                      bucket: int, fn: str = "mean") -> dict[tuple[str, str], list[tuple[datetime, float | None]]]:
        value = self.door.query("data.series", {
            "table": SIGNALS_TABLE, "column": "value", "time_column": "ts",
            "bucket": _interval(bucket), "fn": fn,
            "window": {"column": "ts", "start": _iso(start), "end": _iso(end)},
            "filter": self._filter(site, chans),
            "group_by": ["feed", "channel"],
            # One channel is one quantity. A gold row that left its unit
            # unstated is still that channel's reading, not a second quantity.
            "allow_mixed_units": True,
        })
        out: dict[tuple[str, str], list[tuple[datetime, float | None]]] = {}
        for group in (value.get("data") or {}).get("series") or []:
            key = group.get("key") or {}
            k = (str(key.get("feed") or ""), str(key.get("channel") or ""))
            pts = out.setdefault(k, [])
            for p in group.get("points") or []:
                t = _parse_ts(p.get("t"))
                v = p.get("value")
                if t is None:
                    continue
                try:
                    pts.append((t, None if v is None else float(v)))
                except (TypeError, ValueError):
                    pts.append((t, None))
        return out

    def _chunks(self, start: datetime, end: datetime, bucket: int,
                chunk: timedelta | None = None) -> list[tuple[datetime, datetime]]:
        span = max(chunk or self.chunk, (end - start) / self.max_chunks, timedelta(seconds=bucket))
        # Chunk edges on bucket multiples, so no bucket is split across two reads.
        step = timedelta(seconds=bucket * max(1, int(span.total_seconds() // bucket)))
        edges: list[tuple[datetime, datetime]] = []
        lo = start
        while lo < end:
            hi = min(end, lo + step)
            edges.append((lo, hi))
            lo = hi
        return edges

    def read(self, site: str, chans: list[Channel], start: datetime, end: datetime,
             bucket: int, fn: str = "mean", chunk: timedelta | None = None,
             ) -> dict[tuple[str, str], list[tuple[datetime, float | None]]]:
        chunks = self._chunks(start, end, bucket, chunk)
        if len(chunks) == 1:
            return self._series_chunk(site, chans, start, end, bucket, fn)
        merged: dict[tuple[str, str], list[tuple[datetime, float | None]]] = {}
        with ThreadPoolExecutor(max_workers=min(self.workers, len(chunks))) as pool:
            parts = list(pool.map(lambda c: self._series_chunk(site, chans, c[0], c[1], bucket, fn), chunks))
        for part in parts:
            for k, pts in part.items():
                merged.setdefault(k, []).extend(pts)
        for k in merged:
            seen: dict[datetime, float | None] = {}
            for t, v in merged[k]:
                seen.setdefault(t, v)
            merged[k] = sorted(seen.items())
        return merged

    def _read_paced(self, site: str, chans: list[Channel], start: datetime, end: datetime,
                    bucket: int, fn: str, chunk: timedelta,
                    ) -> dict[tuple[str, str], list[tuple[datetime, float | None]]]:
        """Like :meth:`read`, with a fixed chunk and no cap on how many reads it takes."""
        merged: dict[tuple[str, str], list[tuple[datetime, float | None]]] = {}
        edges, lo = [], start
        while lo < end:
            edges.append((lo, min(end, lo + chunk)))
            lo = edges[-1][1]
        with ThreadPoolExecutor(max_workers=max(1, min(self.workers, len(edges)))) as pool:
            parts = list(pool.map(lambda c: self._series_chunk(site, chans, c[0], c[1], bucket, fn), edges))
        for part in parts:
            for k, pts in part.items():
                merged.setdefault(k, []).extend(pts)
        return merged

    def _channel_days(self, c: Channel) -> dict[str, int]:
        """One channel's readings per day, read from the node."""
        if c.first is None or c.last is None:
            return {}
        start = c.first.replace(hour=0, minute=0, second=0, microsecond=0)
        end = c.last.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        chunk = timedelta(days=max(1, int(self.dates_chunk_rows / (_rows_per_day(c) or 1.0))))
        got = self._read_paced(c.site, [c], start, end, 86400, "count", chunk)
        counts: dict[str, int] = {}
        for pts in got.values():
            for t, v in pts:
                if v:
                    day = t.strftime("%Y-%m-%d")
                    counts[day] = counts.get(day, 0) + int(v)
        return counts

    def _days_store(self) -> dict[str, Any]:
        if not self.dates_cache_path:
            return {}
        try:
            data = json.loads(Path(self.dates_cache_path).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _days_save(self, key: str, at: float, counts: dict[str, int]) -> None:
        if not self.dates_cache_path:
            return
        path = Path(self.dates_cache_path).expanduser()
        with self._lock:
            data = self._days_store()
            data[key] = {"at": at, "days": counts}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(path)

    def _days(self, c: Channel) -> dict[str, int]:
        """A channel's days: fresh from memory, else from disk, else read once.

        A stale answer is served while a fresh one is read behind it, so the
        calendar never waits on a scan it has already done once.
        """
        key = f"{c.site}|{c.feed}|{c.channel}"
        now = time.time()
        with self._lock:
            hit = self._dates.get(key)
        if hit is None:
            stored = self._days_store().get(key)
            if isinstance(stored, dict) and isinstance(stored.get("days"), dict):
                hit = (float(stored.get("at") or 0), {str(k): int(v) for k, v in stored["days"].items()})
                with self._lock:
                    self._dates[key] = hit
        if hit is not None:
            if now - hit[0] > self.dates_ttl:
                self._refresh(key, c, block=False)
            return hit[1]
        return self._refresh(key, c, block=True) or {}

    def _refresh(self, key: str, c: Channel, *, block: bool) -> dict[str, int] | None:
        with self._lock:
            pending = self._inflight.get(key)
            if pending is None:
                pending = self._pool().submit(self._read_days, key, c)
                self._inflight[key] = pending
        return pending.result() if block else None

    def _read_days(self, key: str, c: Channel) -> dict[str, int]:
        try:
            counts = self._channel_days(c)
            at = time.time()
            with self._lock:
                self._dates[key] = (at, counts)
            self._days_save(key, at, counts)
            return counts
        finally:
            with self._lock:
                self._inflight.pop(key, None)

    def _pool(self) -> ThreadPoolExecutor:
        if self._background is None:
            self._background = ThreadPoolExecutor(max_workers=2, thread_name_prefix="figure-days")
        return self._background

    def dates(self, site: str, tokens: Iterable[str], feed: str = "") -> list[dict[str, Any]]:
        """The days these channels have readings on, with how many. Cached per channel."""
        counts: dict[str, int] = {}
        for c in self.resolve(site, tokens, feed):
            for day, n in self._days(c).items():
                counts[day] = counts.get(day, 0) + n
        return [{"date": d, "rows": n} for d, n in sorted(counts.items())]

    def warm(self, site: str, tokens: Iterable[str], feed: str = "") -> None:
        """Read these channels' days in the background, so the first calendar is instant."""
        for c in self.resolve(site, tokens, feed):
            key = f"{c.site}|{c.feed}|{c.channel}"
            with self._lock:
                hit = self._dates.get(key)
            if hit is None:
                stored = self._days_store().get(key)
                if isinstance(stored, dict) and time.time() - float(stored.get("at") or 0) < self.dates_ttl:
                    continue
            elif time.time() - hit[0] < self.dates_ttl:
                continue
            self._refresh(key, c, block=False)

    # --- the figure -----------------------------------------------------------
    def window(self, chans: list[Channel], t_from: datetime | None, t_to: datetime | None,
               last: timedelta | None) -> tuple[datetime, datetime, list[str]]:
        notes: list[str] = []
        data_last = max((c.last for c in chans if c.last), default=datetime.now(UTC))
        end = t_to or data_last
        if t_from:
            start = t_from
        elif last:
            start = end - last
        else:
            # Open on when these channels were last doing something: their last day.
            start = end - timedelta(days=1)
            notes.append("opened on when these channels were last doing something")
        if end <= start:
            end = start + timedelta(hours=1)
        per_day = sum(_rows_per_day(c) for c in chans)
        allowed = max(self.min_window, timedelta(days=self.row_budget / per_day)) if per_day else None
        if allowed is not None and end - start > allowed:
            start = end - allowed
            notes.append(
                f"showing the last {_span_words(allowed)} of the window asked for, which is as "
                "much as these channels draw quickly; narrow the window to see earlier readings"
            )
        return start, end, notes

    def figure(
        self,
        site: str,
        tokens: Iterable[str],
        *,
        feed: str = "",
        t_from: datetime | None = None,
        t_to: datetime | None = None,
        last: timedelta | None = None,
        width: int = 0,
        height: int = 0,
        palette: list[str] | None = None,
        held_extent: Mapping[str, tuple[float, float]] | None = None,
        title: str = "",
        subtitle: str = "",
    ) -> dict[str, Any]:
        """The Figure JSON the appkit ``FigureSource`` contract draws."""
        tokens = list(tokens)
        chans = self.resolve(site, tokens, feed)
        if not chans:
            raise FigureNote(f"no readings for {','.join(tokens)!r} at {site}")
        axis_unit = chans[0].unit
        drawn = [c for c in chans if c.unit == axis_unit]
        skipped = [c.channel for c in chans if c.unit != axis_unit]
        start, end, notes = self.window(drawn, t_from, t_to, last)
        bucket = choose_bucket((end - start).total_seconds(), self.target_points)
        got = self.read(site, drawn, start, end, bucket)
        series_objs: list[Series] = []
        readout: list[dict[str, Any]] = []
        for c in drawn:
            pts = got.get((c.feed, c.channel), [])
            series_objs.append(Series(name=c.channel, points=pts, unit=c.unit, mark="line"))
            readout.append({
                "name": c.channel, "unit": c.unit,
                "points": [[int(t.timestamp() * 1000), v] for t, v in pts if v is not None],
            })
        if not any(r["points"] for r in readout):
            raise FigureNote(f"nothing to draw for {', '.join(c.channel for c in drawn)} "
                             f"between {_iso(start)} and {_iso(end)}")
        colours = ({c.channel: palette[i % len(palette)] for i, c in enumerate(drawn)}
                   if palette else None)
        svg = render_svg(
            series_objs,
            title=title or ", ".join(c.channel for c in drawn),
            subtitle=subtitle,
            provenance=f"{site} · gold.{SIGNALS_TABLE} · {_interval(bucket)} means",
            geometry=Geometry.for_width(width, height=height) if width else None,
            series_colours=colours,
            held_extent=held_extent or None,
            window=(start, end),
        )
        firsts = [c.first for c in drawn if c.first]
        lasts = [c.last for c in drawn if c.last]
        extent: dict[str, list[float]] = {}
        for so in series_objs:
            vals = [v for _, v in so.points if v is not None and math.isfinite(v)]
            if vals and so.unit:
                lo, hi = extent.get(so.unit, [vals[0], vals[0]])
                extent[so.unit] = [min(lo, *vals), max(hi, *vals)]
        if skipped:
            notes.append(f"left out {', '.join(skipped)}: a different unit from {axis_unit or 'the first series'}")
        window = {"from": start.isoformat(), "to": end.isoformat()}
        return {
            "svg": svg,
            "window": window,
            "bounds": {
                "from": (min(firsts) if firsts else start).isoformat(),
                "to": (max(lasts) if lasts else end).isoformat(),
            },
            "units": sorted({c.unit for c in drawn if c.unit}),
            "extent": extent,
            "readout": {"from": window["from"], "to": window["to"], "series": readout},
            "kind": "line",
            "notes": notes,
            "served": {"bucket": _interval(bucket), "source": f"gold.{SIGNALS_TABLE}"},
        }


__all__ = [
    "Channel",
    "DataDoor",
    "FigureNote",
    "NodeFigureSource",
    "NodeReadError",
    "choose_bucket",
    "key_from_vault",
]
