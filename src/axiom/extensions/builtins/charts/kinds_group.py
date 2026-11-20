# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The chart-kinds group: one chart of every ChartSpec 2.0 kind, drawn from a site's own gold.

A site hands over which of its catalog channels each kind should draw (a small
TOML document, :class:`KindsConfig`); this module turns that into a saved group
of graphs on the Charts page, one per registered kind, and answers each graph's
data when the renderer asks for it.

**Nothing is copied into the saved view.** Each graph's spec carries
``data.verb = {"name": "charts.kinds_data", "params": {"site", "kind"}}`` and the
renderer resolves it from gold on every draw (:func:`resolve`), so the group is
a few kilobytes however much it draws, and a refresh is the next draw.

**A replay of a recorded day.** The kinds that read one day (lines, areas, steps,
the sparkline, the tiles, the gauge) carry a playback control over the day in
``frames`` steps; each step draws the day up to that moment, and the control
loops. Every subtitle says which day it is a replay of. A day is only replayed
once it is ``delay_hours`` old.

**Honest about what it is.** Each chart's ``catalog.origin`` names the gold it
read (an export, a snapshot, a node); quality verdicts are drawn as the neutral
hatch (MVP-03-A4) and counted where the form carries them; a modelled series is
its measured counterpart's hue, dashed and labelled (MVP-03-A3). A kind with no
real source is drawn from generated values only when the config asks for that,
and its title, subtitle and lineage all say SAMPLE.

**Its own platform timing.** The flame graph is the platform's capability
telemetry (``telemetry/capabilities.jsonl``): every capability call's latency,
by surface and extension, over the last days.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import threading
import tomllib
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .kinds_sources import GoldSource, Reading, SourceError, day_bounds, open_source, split_ref

#: The verb every graph in the group names; :func:`resolve` answers it.
VERB = "charts.kinds_data"
#: Where a registered site config is kept, under the state directory.
CONFIG_DIR = ("charts", "kinds")
#: The playback control's id and the param it moves.
PLAY_ID = "replay"
PLAY_PARAM = "at"
#: The order the group lays its graphs in.
ORDER = (
    "line", "area", "step", "sparkline", "kpi", "gauge", "bullet", "progress", "state", "pie",
    "donut", "scatter", "histogram", "box", "heatmap", "bar", "flame", "mesh3d", "surface",
    "scatter3d", "volume",
)
#: Kinds drawn from the replay day, which carry the playback control.
REPLAYED = frozenset({"line", "area", "step", "sparkline", "kpi", "gauge", "state"})
SAMPLE = "SAMPLE"


class KindsError(ValueError):
    """A config or a request the group cannot be built from, and why."""


@dataclass(frozen=True)
class Chan:
    ref: str
    label: str
    unit: str = ""
    role: str = "measured"
    counterpart_of: str = ""
    sigma: str = ""
    #: A reading at or below this is the source's no-reading value, not a measurement: withheld.
    valid_above: float | None = None
    #: Readings are multiplied by this before they are drawn, with ``unit`` naming the result:
    #: a source in W drawn in MW says ``scale = 1e-6, unit = "MW"``. Every threshold the same
    #: recipe states (max, limit) is in the drawn unit.
    scale: float = 1.0

    @property
    def feed(self) -> str:
        return split_ref(self.ref)[0]

    @property
    def channel(self) -> str:
        return split_ref(self.ref)[1]


def _chan(raw: Any, where: str) -> Chan:
    if isinstance(raw, str):
        return Chan(ref=raw, label=split_ref(raw)[1])
    if not isinstance(raw, Mapping) or not raw.get("ref"):
        raise KindsError(f"{where}: a channel is {{ref = 'feed:channel', label = '...'}}")
    split_ref(str(raw["ref"]))
    return Chan(ref=str(raw["ref"]), label=str(raw.get("label") or split_ref(str(raw["ref"]))[1]),
                unit=str(raw.get("unit") or ""), role=str(raw.get("role") or "measured"),
                counterpart_of=str(raw.get("counterpart_of") or ""), sigma=str(raw.get("sigma") or ""),
                valid_above=float(raw["valid_above"]) if raw.get("valid_above") is not None else None,
                scale=float(raw.get("scale") or 1.0))


@dataclass
class KindsConfig:
    """What a site says each kind should draw. Domain-free: channels, thresholds, labels."""

    site: str
    label: str
    group: str
    replay: date
    sources: dict[str, dict[str, Any]]
    kinds: dict[str, dict[str, Any]]
    delay_hours: float = 24.0
    frames: int = 48
    #: The replay step the page opens on; None opens on the last (the whole day). A day that ends
    #: shut down reads as zero there, so a site that wants the demo at power names a step.
    open_frame: int | None = None
    week: tuple[date, date] | None = None
    span: tuple[date, date] | None = None
    base: Path = field(default_factory=Path.cwd)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, base: Path | None = None) -> KindsConfig:
        head = raw.get("kinds_group") or {}
        site = str(head.get("site") or "")
        if not site:
            raise KindsError("kinds_group.site: which site the group is drawn from")
        try:
            replay = date.fromisoformat(str(head.get("replay") or ""))
        except ValueError:
            raise KindsError("kinds_group.replay: the recorded day to replay, as YYYY-MM-DD") from None

        def _span(key: str) -> tuple[date, date] | None:
            got = head.get(key)
            if not got:
                return None
            a, b = (date.fromisoformat(str(x)) for x in got)
            return (a, b)

        kinds = {str(k): dict(v) for k, v in (raw.get("kinds") or {}).items()}
        from axiom.extensions.builtins.scidisplay.unified_spec import registered_kinds

        unknown = sorted(set(kinds) - set(registered_kinds()))
        if unknown:
            raise KindsError(f"kinds: {', '.join(unknown)} is not a ChartSpec kind")
        return cls(site=site, label=str(head.get("label") or site), group=str(head.get("group") or "Chart kinds"),
                   replay=replay, sources={str(k): dict(v) for k, v in (raw.get("sources") or {}).items()},
                   kinds=kinds, delay_hours=float(head.get("delay_hours", 24)),
                   frames=max(2, int(head.get("frames", 48))),
                   open_frame=int(head["open_frame"]) if head.get("open_frame") is not None else None,
                   week=_span("week"), span=_span("span"), base=base or Path.cwd())

    @classmethod
    def load(cls, path: str | Path) -> KindsConfig:
        p = Path(path).expanduser().resolve()
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise KindsError(f"{p}: {exc}") from exc
        return cls.from_mapping(raw, base=p.parent)

    def as_mapping(self) -> dict[str, Any]:
        """The config with every relative path made absolute: what is registered."""

        def _abs(spec: Mapping[str, Any]) -> dict[str, Any]:
            out = dict(spec)
            for key in ("export", "snapshot"):
                if out.get(key):
                    p = Path(str(out[key])).expanduser()
                    out[key] = str(p if p.is_absolute() else (self.base / p).resolve())
            return out

        kinds = {}
        for k, v in self.kinds.items():
            v = dict(v)
            for key in ("scene", "acceptance"):
                if v.get(key):
                    p = Path(str(v[key])).expanduser()
                    v[key] = str(p if p.is_absolute() else (self.base / p).resolve())
            kinds[k] = v
        head: dict[str, Any] = {"site": self.site, "label": self.label, "group": self.group,
                                "replay": self.replay.isoformat(), "delay_hours": self.delay_hours,
                                "frames": self.frames}
        if self.open_frame is not None:
            head["open_frame"] = self.open_frame
        if self.week:
            head["week"] = [d.isoformat() for d in self.week]
        if self.span:
            head["span"] = [d.isoformat() for d in self.span]
        return {"kinds_group": head, "sources": {k: _abs(v) for k, v in self.sources.items()}, "kinds": kinds}

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.as_mapping(), sort_keys=True).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Registration: where the renderer finds a site's config at draw time.
# ---------------------------------------------------------------------------


def _config_path(state_dir: Path, site: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in site)
    return Path(state_dir).joinpath(*CONFIG_DIR, f"{safe}.json")


def register(cfg: KindsConfig, state_dir: Path) -> Path:
    path = _config_path(state_dir, cfg.site)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.as_mapping(), indent=2, sort_keys=True), encoding="utf-8")
    return path


def registered(site: str, state_dir: Path) -> KindsConfig | None:
    path = _config_path(state_dir, site)
    if not path.exists():
        return None
    return KindsConfig.from_mapping(json.loads(path.read_text(encoding="utf-8")), base=path.parent)


def registered_all(state_dir: Path) -> list[KindsConfig]:
    """Every config registered on this node, by site: the first provider of prebuilt groups."""
    root = Path(state_dir).joinpath(*CONFIG_DIR)
    out: list[KindsConfig] = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            out.append(KindsConfig.from_mapping(json.loads(path.read_text(encoding="utf-8")), base=path.parent))
        except (OSError, ValueError):
            continue  # one unreadable registration does not hide the others
    return out


# ---------------------------------------------------------------------------
# The group.
# ---------------------------------------------------------------------------


def check_delay(day: date, delay_hours: float, *, now: datetime | None = None) -> None:
    """Refuse a day that is not yet ``delay_hours`` old (a public replay waits that long)."""
    end = day_bounds(day)[1]
    now = now or datetime.now(UTC)
    if end + timedelta(hours=delay_hours) > now:
        raise KindsError(f"{day.isoformat()} is not {delay_hours:g} h old yet; a replay waits that long")


def _spec(cfg: KindsConfig, kind: str, title: str, subtitle: str, *, replay: date,
          extra: Mapping[str, Any] | None = None, provenance: str | None = "measured",
          origin: str = "", notes: str = "") -> dict[str, Any]:
    from axiom.extensions.builtins.scidisplay.unified_spec import SCHEMA_VERSION

    params: dict[str, Any] = {"site": cfg.site, "kind": kind, "replay": replay.isoformat()}
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": f"kinds-{cfg.site}-{kind}".lower(),
        "kind": kind,
        "title": title,
        "data": {"verb": {"name": VERB, "params": params}},
    }
    if subtitle:
        doc["subtitle"] = subtitle
    if kind in REPLAYED:
        doc["controls"] = [{
            "id": PLAY_ID, "type": "playback", "label": f"Replay {replay.isoformat()}",
            "bind": {"to": "param", "key": PLAY_PARAM}, "min": 0, "max": cfg.frames - 1, "step": 1,
            "value": min(cfg.frames - 1, max(0, cfg.open_frame if cfg.open_frame is not None else cfg.frames - 1)),
            "interval_ms": 700, "loop": True,
        }]
    catalog: dict[str, Any] = {"group": cfg.group, "order": ORDER.index(kind) if kind in ORDER else 99,
                               "origin": origin, "verb": VERB}
    if provenance:
        catalog["provenance"] = provenance
    if notes:
        catalog["notes"] = notes
    doc["catalog"] = catalog
    for k, v in (extra or {}).items():
        doc[k] = v
    return doc


#: Titles when a kind's config names none. Plain, domain-free.
_DEFAULT_TITLES = {
    "line": "Readings over the day", "area": "Level over the day", "step": "Positions over the day",
    "sparkline": "The day at a glance", "kpi": "Now", "gauge": "Against the limit",
    "bullet": "Hours at level against target", "progress": "Progress", "state": "State over the day",
    "pie": "Share of the day in each state", "donut": "Quality verdicts", "scatter": "One against another",
    "histogram": "Distribution", "box": "Spread per day", "heatmap": "By hour and day",
    "bar": "Hours at level per day", "flame": "Platform timing", "mesh3d": "Geometry",
    "surface": "By hour and day, as a surface", "scatter3d": "Three readings at once",
    "volume": "A field in 3D",
}


def _origin(cfg: KindsConfig, which: str) -> str:
    spec = cfg.sources.get(which)
    if not spec:
        return ""
    try:
        return open_source(spec, cfg.site, base=cfg.base).lineage()
    except SourceError as exc:
        return f"unavailable: {exc}"


def build_group(cfg: KindsConfig, *, replay: date | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The saved group: ``{id, name, graphs, layout, ...}`` with one graph per configured kind."""
    day = replay or cfg.replay
    check_delay(day, cfg.delay_hours, now=now)
    graphs: list[dict[str, Any]] = []
    replay_note = f"replay of {day.isoformat()}"
    for kind in ORDER:
        rc = cfg.kinds.get(kind)
        if rc is None:
            continue
        title = str(rc.get("title") or _DEFAULT_TITLES.get(kind, kind))
        sub = str(rc.get("subtitle") or "")
        origin, provenance, notes = "", "measured", ""
        if kind == "flame":
            origin, provenance = "platform capability telemetry (telemetry/capabilities.jsonl)", None
        elif kind == "mesh3d":
            origin, provenance = str(rc.get("lineage") or "the site's as-built geometry"), None
        elif rc.get("sample"):
            title = f"{SAMPLE} · {title}"
            origin, provenance, notes = f"{SAMPLE}: generated values; no real source for this kind", None, SAMPLE
        elif kind == "progress":
            origin, provenance = _origin(cfg, "history"), "hybrid"
        else:
            which = "day" if kind in REPLAYED or kind in ("donut", "histogram", "pie") else "history"
            origin = _origin(cfg, which)
            if any(_chan(c, kind).role == "simulated" for c in rc.get("channels") or []):
                provenance = "hybrid"
        if kind in REPLAYED or kind in ("donut", "histogram", "pie"):
            sub = f"{sub} · {replay_note}" if sub else replay_note
        if rc.get("target", {}).get("sample") if isinstance(rc.get("target"), Mapping) else False:
            notes = f"target is {SAMPLE}"
        extra: dict[str, Any] = {}
        if kind in ("scatter",):
            extra["encoding"] = {"fit": "linear"}
        if kind == "mesh3d":
            extra["encoding"] = {"camera": "iso", "cutaway": False}
        spec = _spec(cfg, kind, title, sub, replay=day, extra=extra, provenance=provenance,
                     origin=origin, notes=notes)
        graphs.append({
            # A spec graph names its spec as its one series, as a chart saved from chat does: the
            # page draws a graph only when it names something to draw.
            "id": f"g{len(graphs) + 1}", "site": cfg.site, "feeds": [], "series": [spec["id"]], "name": title,
            "kind": kind, "spec": spec, "drawer": False,
        })
    if not graphs:
        raise KindsError("the config names no kinds")
    return {"id": f"kinds-{cfg.site}", "name": f"{cfg.group} · {cfg.label}", "graphs": graphs, "layout": "3",
            "syncDates": False, "syncRun": False, "last": "", "window": None, "expanded": None}


def merge_into(document: Mapping[str, Any] | None, group: Mapping[str, Any]) -> dict[str, Any]:
    """The saved Charts view with ``group`` in it: replacing a group of the same name, else appended."""
    doc = dict(document or {})
    groups = [dict(g) for g in (doc.get("groups") or []) if isinstance(g, Mapping)]
    if not groups and doc.get("graphs"):
        # A view saved before sections: its fields are its one group. Keep it as that group.
        groups = [{"id": "k1", "name": doc.get("name") or "", "graphs": doc.get("graphs") or [],
                   "layout": doc.get("layout") or "auto", "syncDates": bool(doc.get("syncDates")),
                   "syncRun": bool(doc.get("syncRun")), "last": doc.get("last") or "",
                   "window": doc.get("window"), "expanded": doc.get("expanded")}]
    taken = {str(g.get("id")) for g in groups}
    at = next((i for i, g in enumerate(groups) if g.get("name") == group["name"]), None)
    new = dict(group)
    if at is not None:
        new["id"] = groups[at].get("id") or new["id"]
        groups[at] = new
    else:
        n = 1
        while f"k{n}" in taken:
            n += 1
        new["id"] = f"k{n}"
        groups.append(new)
    doc["groups"] = groups
    doc["display"] = new["id"]
    return doc


# ---------------------------------------------------------------------------
# Answering a graph's data at draw time.
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_sources: dict[tuple[str, str], GoldSource] = {}
_reads: dict[tuple[Any, ...], list[Reading]] = {}
_READ_CAP = 256


def _source(cfg: KindsConfig, which: str) -> GoldSource:
    spec = cfg.sources.get(which) or cfg.sources.get("day") or cfg.sources.get("history")
    if not spec:
        raise KindsError(f"sources.{which}: where this kind reads its gold")
    key = (cfg.digest(), which)
    with _lock:
        got = _sources.get(key)
    if got is None:
        got = open_source(spec, cfg.site, base=cfg.base)
        with _lock:
            _sources[key] = got
    return got


def _read(cfg: KindsConfig, which: str, ch: Chan, start: datetime, end: datetime, step: int) -> list[Reading]:
    key = (cfg.digest(), which, ch.ref, ch.scale, ch.valid_above, start.isoformat(), end.isoformat(), step)
    with _lock:
        if key in _reads:
            return _reads[key]
    got = _source(cfg, which).read(ch.feed, ch.channel, start, end, step)
    if ch.valid_above is not None:
        floor = ch.valid_above
        got = [(t, None, "bad") if v is not None and v <= floor else (t, v, q) for t, v, q in got]
    if ch.scale != 1.0:
        got = [(t, None if v is None else v * ch.scale, q) for t, v, q in got]
    with _lock:
        if len(_reads) >= _READ_CAP:
            _reads.pop(next(iter(_reads)))
        _reads[key] = got
    return got


def clear_cache() -> None:
    with _lock:
        _sources.clear()
        _reads.clear()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


def _unit(cfg: KindsConfig, which: str, ch: Chan) -> str:
    return ch.unit or _source(cfg, which).unit(ch.feed, ch.channel)


def _cursor(cfg: KindsConfig, day: date, at: Any) -> tuple[datetime, datetime, datetime]:
    start, end = day_bounds(day)
    try:
        k = int(at)
    except (TypeError, ValueError):
        k = cfg.frames - 1
    k = max(0, min(cfg.frames - 1, k))
    cursor = start + (end - start) * (k + 1) / cfg.frames
    return start, end, cursor


def _frame_index(cfg: KindsConfig | None, at: Any) -> int | None:
    """The replay step asked for, or None for the whole day (the last step, or none asked)."""
    if cfg is None:
        return None
    try:
        k = int(at)
    except (TypeError, ValueError):
        return None
    return None if k >= cfg.frames - 1 else max(0, k)


def _hatches(readings: Sequence[Reading], name: str, step: int) -> list[dict[str, Any]]:
    """Quality verdicts as the neutral hatch (MVP-03-A4): held, stale and suspect spans."""
    from axiom.extensions.builtins.data_platform.marks import mark_spans

    pts = [{"t": t, "value": v, "quality": q} for t, v, q in readings]
    out = []
    for span in mark_spans(pts):
        if span["mark"] not in ("held", "stale", "suspect", "gap"):
            continue
        out.append({"type": "hatch", "axis": "x", "from": _iso(span["start"]), "to": _iso(span["end"] + step),
                    "label": "—", "title": f"{name} {span['mark']}: {span['reason']}"})
    return out


def _day_series(cfg: KindsConfig, rc: Mapping[str, Any], day: date, at: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], datetime]:
    start, end, cursor = _cursor(cfg, day, at)
    step = int(rc.get("step_s") or 60)
    series, marks = [], []
    for raw in rc.get("channels") or ([rc["channel"]] if rc.get("channel") else []):
        ch = _chan(raw, "channels")
        got = [r for r in _read(cfg, "day", ch, start, end, step) if r[0] < cursor.timestamp()]
        s: dict[str, Any] = {"name": ch.label, "unit": _unit(cfg, "day", ch),
                             "x": [_iso(t) for t, _, _ in got], "y": [None if v is None else round(v, 4) for _, v, _ in got]}
        if ch.role == "simulated":
            s["role"] = "simulated"
            if ch.counterpart_of:
                s["counterpart_of"] = ch.counterpart_of
        if ch.sigma:
            sig = {round(t): v for t, v, _ in _read(cfg, "day", replace(_chan(ch.sigma, "sigma"), scale=ch.scale), start, end, step)}
            lo, hi = [], []
            for (t, v, _) in got:
                e = sig.get(round(t))
                lo.append(None if v is None or e is None else round(v - e, 4))
                hi.append(None if v is None or e is None else round(v + e, 4))
            s["low"], s["high"] = lo, hi
        series.append(s)
        marks.extend(_hatches(got, ch.label, step))
    return series, marks, cursor


def _states(rc: Mapping[str, Any]) -> list[tuple[str, float | None]]:
    out = []
    for s in rc.get("states") or []:
        out.append((str(s["name"]), float(s["below"]) if s.get("below") is not None else None))
    if not out:
        raise KindsError("states: [{name, below}, ..., {name}] from lowest to highest")
    return out


def _state_of(v: float | None, states: list[tuple[str, float | None]]) -> int | None:
    if v is None:
        return None
    for i, (_, below) in enumerate(states):
        if below is None or v < below:
            return i
    return len(states) - 1


def _span_days(span: tuple[date, date] | None, fallback: date) -> list[date]:
    a, b = span or (fallback - timedelta(days=6), fallback)
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def _history(cfg: KindsConfig, ch: Chan, days: list[date], step: int = 300) -> list[Reading]:
    start = day_bounds(days[0])[0]
    end = day_bounds(days[-1])[1]
    return _read(cfg, "history", ch, start, end, step)


def _hours_at(readings: Sequence[Reading], at_least: float, step: int) -> float:
    return sum(step for _, v, _ in readings if v is not None and v >= at_least) / 3600.0


def _where(cfg: KindsConfig, rc: Mapping[str, Any], which: str, start: datetime, end: datetime,
           step: int) -> set[int] | None:
    """Bucket times (rounded seconds) at which the ``where`` channel is at least its threshold."""
    w = rc.get("where")
    if not w:
        return None
    ch = _chan(w, "where")
    keep = {round(t) for t, v, _ in _read(cfg, which, ch, start, end, step) if v is not None and v >= float(w["at_least"])}
    return keep


def _kind_data(cfg: KindsConfig, kind: str, day: date, params: Mapping[str, Any]) -> dict[str, Any]:
    rc = cfg.kinds.get(kind)
    if rc is None:
        raise KindsError(f"{cfg.site} has no {kind} in its chart-kinds config")
    at = params.get(PLAY_PARAM)
    if kind in ("line", "area", "step", "sparkline"):
        series, marks, cursor = _day_series(cfg, rc, day, at)
        out: dict[str, Any] = {"series": series}
        if kind != "sparkline" and marks:
            out["marks"] = marks
        return out
    if kind == "kpi":
        series, _, cursor = _day_series(cfg, rc, day, at)
        for s in series:
            ys = [v for v in s["y"] if v is not None]
            s["y"] = ys[-60:] if ys else [None]
            s.pop("x", None)
            for k in ("low", "high"):
                s.pop(k, None)
        return {"series": series}
    if kind == "gauge":
        series, _, cursor = _day_series(cfg, {**rc, "channels": [rc["channel"]]}, day, at)
        ys = [v for v in series[0]["y"] if v is not None]
        limit = rc.get("limit") or {}
        value: dict[str, Any] = {"value": ys[-1] if ys else None, "unit": series[0]["unit"],
                                 "min": float(rc.get("min", 0)), "max": float(rc["max"]),
                                 "label": f"{series[0]['name']} at {cursor.strftime('%H:%M')} UTC"}
        if limit.get("value") is not None:
            value["target"] = float(limit["value"])
        return {"value": value}
    if kind == "state":
        states = _states(cfg.kinds.get("state") or rc)
        series, _, _ = _day_series(cfg, {**rc, "channels": [rc["channel"]]}, day, at)
        s = series[0]
        s["y"] = [_state_of(v, states) for v in s["y"]]
        s["name"] = "State"
        s["unit"] = ""
        return {"series": [s]}
    if kind == "pie":
        src = cfg.kinds.get("state") or rc
        states = _states(src)
        ch = _chan(src["channel"], "state.channel")
        start, end = day_bounds(day)
        step = int(src.get("step_s") or 60)
        counts = Counter(_state_of(v, states) for _, v, _ in _read(cfg, "day", ch, start, end, step))
        names = [n for n, _ in states]
        hours = [round(counts.get(i, 0) * step / 3600.0, 2) for i in range(len(names))]
        return {"series": [{"name": "Hours", "unit": "h", "x": names, "y": hours}]}
    if kind == "donut":
        start, end = day_bounds(day)
        src = _source(cfg, "day")
        if not src.carries_quality():
            raise KindsError("the day's source carries no quality verdicts to count")
        verdicts: Counter[str] = Counter()
        for raw in rc.get("channels") or []:
            ch = _chan(raw, "channels")
            for _, v, q in _read(cfg, "day", ch, start, end, 0):
                # The verdict as gold states it; a reading with neither is said so, never "good".
                verdicts[q or ("no value" if v is None else "no verdict")] += 1
        names = sorted(verdicts, key=lambda k: -verdicts[k])
        return {"series": [{"name": "Readings", "unit": "readings", "x": names, "y": [verdicts[k] for k in names]}]}
    if kind == "histogram":
        start, end = day_bounds(day)
        step = int(rc.get("step_s") or 10)
        ch = _chan(rc["channel"], "channel")
        keep = _where(cfg, rc, "day", start, end, step)
        got = _read(cfg, "day", ch, start, end, step)
        ys = [round(v, 3) for t, v, _ in got if v is not None and (keep is None or round(t) in keep)]
        return {"series": [{"name": ch.label, "unit": _unit(cfg, "day", ch), "y": ys}]}
    days = _span_days(cfg.week, day)
    if kind == "scatter":
        x, y = _chan(rc["x"], "x"), _chan(rc["y"], "y")
        xs = {round(t): v for t, v, _ in _history(cfg, x, days) if v is not None}
        pts = [(xs[round(t)], v) for t, v, _ in _history(cfg, y, days) if v is not None and round(t) in xs]
        at_least = rc.get("x_at_least")
        if at_least is not None:
            pts = [p for p in pts if p[0] >= float(at_least)]
        return {"series": [{"name": y.label, "unit": _unit(cfg, "history", y),
                            "x": [round(a, 3) for a, _ in pts], "y": [round(b, 3) for _, b in pts]}]}
    if kind == "box":
        out_series = []
        for raw in rc.get("channels") or []:
            ch = _chan(raw, "channels")
            for d in days:
                start, end = day_bounds(d)
                keep = _where(cfg, rc, "history", start, end, 300)
                ys = [round(v, 3) for t, v, _ in _read(cfg, "history", ch, start, end, 300)
                      if v is not None and (keep is None or round(t) in keep)]
                if ys:
                    out_series.append({"name": f"{ch.label} {d.strftime('%m-%d')}", "unit": _unit(cfg, "history", ch), "y": ys})
        return {"series": out_series}
    span_days = _span_days(cfg.span or cfg.week, day)
    if kind in ("heatmap", "surface"):
        ch = _chan(rc["channel"], "channel")
        grid: dict[tuple[date, int], list[float]] = defaultdict(list)
        for t, v, _ in _history(cfg, ch, span_days):
            if v is None:
                continue
            m = datetime.fromtimestamp(t, UTC)
            grid[(m.date(), m.hour)].append(v)
        rows = [d for d in span_days if any((d, h) in grid for h in range(24))]
        z = [[round(statistics.fmean(grid[(d, h)]), 2) if grid.get((d, h)) else None for h in range(24)] for d in rows]
        unit = _unit(cfg, "history", ch)
        if kind == "heatmap":
            return {"field": {"x": list(range(24)), "y": [d.isoformat() for d in rows], "z": z, "unit": unit}}
        return {"scene": {"grid": {"x": list(range(24)), "y": list(range(len(rows))),
                                   "z": [[0.0 if v is None else v for v in row] for row in z]},
                          "length_unit": "", "scalars": {ch.label: {"unit": unit, "association": "point",
                                                                    "values": [0.0 if v is None else v for row in z for v in row]}}}}
    if kind == "bar":
        ch = _chan(rc["channel"], "channel")
        at_least = float(rc["at_least"])
        xs, ys = [], []
        for d in span_days:
            start, end = day_bounds(d)
            got = _read(cfg, "history", ch, start, end, 300)
            if got:
                xs.append(d.strftime("%m-%d"))
                ys.append(round(_hours_at(got, at_least, 300), 2))
        return {"series": [{"name": str(rc.get("label") or "Hours"), "unit": "h", "x": xs, "y": ys}]}
    if kind == "bullet":
        ch = _chan(rc["channel"], "channel")
        hours = round(_hours_at(_history(cfg, ch, days), float(rc["at_least"]), 300), 1)
        target = rc.get("target") or {}
        value = {"value": hours, "unit": "h", "min": 0.0, "max": float(rc.get("max") or max(hours, float(target.get("value") or 1)) * 1.2),
                 "label": str(rc.get("label") or "Hours"), "ranges": [float(r) for r in rc.get("ranges") or []]}
        if target.get("value") is not None:
            value["target"] = float(target["value"])
        return {"value": value}
    if kind == "scatter3d":
        chans = [_chan(c, "channels") for c in rc.get("channels") or []]
        if len(chans) != 3:
            raise KindsError("scatter3d.channels: exactly three, for x, y and z")
        cols = [{round(t): v for t, v, _ in _history(cfg, c, days) if v is not None} for c in chans]
        keys = sorted(set(cols[0]) & set(cols[1]) & set(cols[2]))
        at_least = rc.get("x_at_least")
        if at_least is not None:
            keys = [k for k in keys if cols[0][k] >= float(at_least)]
        keys = keys[:: max(1, len(keys) // 1500)]
        return {"scene": {"points": [[round(cols[0][k], 3), round(cols[1][k], 3), round(cols[2][k], 3)] for k in keys],
                          "scalars": {chans[2].label: {"unit": _unit(cfg, "history", chans[2]), "association": "point",
                                                       "values": [round(cols[2][k], 3) for k in keys]}}}}
    if kind == "progress":
        rows = []
        for row in rc.get("rows") or []:
            if row.get("acceptance"):
                got = _acceptance_row(row)
                if got:
                    rows.append(got)
                continue
            ch = _chan(row["channel"], "rows.channel")
            hours = round(_hours_at(_history(cfg, ch, span_days), float(row["at_least"]), 300), 1)
            plan = float(row["plan"])
            rows.append({"name": str(row.get("label") or "Hours") + (f" (plan {SAMPLE})" if row.get("plan_sample") else ""),
                         "unit": "h", "y": [hours], "low": 0.0, "high": plan})
        return {"series": rows}
    if kind == "flame":
        return {"tree": {"root": platform_timing(_state_dir(params), days=int(rc.get("days") or 7))}}
    if kind == "mesh3d":
        path = Path(str(rc.get("scene") or ""))
        try:
            scene = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise KindsError(f"mesh3d.scene: {path} cannot be read ({exc})") from exc
        return {"scene": scene}
    if kind == "volume":
        return {"scene": sample_field(int(rc.get("n") or 9))}
    raise KindsError(f"no recipe for {kind}")


def _acceptance_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Rows proven of the acceptance spec (``axi app acceptance``), as one progress row."""
    try:
        from axiom.extensions.builtins.app_buildout.acceptance import build_report, resolve_config

        report = build_report(resolve_config(str(row["acceptance"])))
    except Exception:  # noqa: BLE001 - an unreadable harness is a missing row, said in notes
        return None
    prefix = str(row.get("prefix") or "")
    rows = [r for r in report.get("rows") or [] if str(r.get("id", "")).startswith(prefix)]
    proven = sum(1 for r in rows if r.get("status") == "pass")
    return {"name": str(row.get("label") or "Acceptance rows proven"), "unit": "rows",
            "y": [proven], "low": 0.0, "high": float(len(rows) or 1)}


def _state_dir(params: Mapping[str, Any]) -> Path:
    raw = params.get("_state_dir")
    if raw:
        return Path(str(raw))
    from axiom.infra.capability_telemetry import default_state_dir

    return default_state_dir()


def platform_timing(state_dir: Path, *, days: int = 7, now: float | None = None) -> dict[str, Any]:
    """The platform's own capability latency as a tree: surface → extension → capability, in ms."""
    import time as _time

    from axiom.infra.capability_telemetry import telemetry_path

    path = telemetry_path(state_dir)
    since = (now or _time.time()) - days * 86400
    totals: dict[tuple[str, str, str], float] = defaultdict(float)
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if float(e.get("ts") or 0) < since or not isinstance(e.get("latency_ms"), int | float):
                    continue
                name = str(e.get("tool_name") or "unknown")
                ext = name.split(".", 1)[0]
                totals[(str(e.get("surface") or "unknown"), ext, name)] += float(e["latency_ms"])
    except OSError:
        pass
    if not totals:
        raise KindsError(f"no capability telemetry in the last {days} days at {path}")
    tree: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for (surface, ext, name), ms in totals.items():
        tree[surface][ext][name] = ms
    children = []
    for surface, exts in sorted(tree.items()):
        ext_nodes = []
        for ext, caps in sorted(exts.items()):
            ext_nodes.append({"name": ext, "value": round(sum(caps.values()), 1), "category": surface,
                              "children": [{"name": n, "value": round(v, 1), "category": surface}
                                           for n, v in sorted(caps.items(), key=lambda kv: -kv[1])]})
        children.append({"name": surface, "value": round(sum(n["value"] for n in ext_nodes), 1),
                         "category": surface, "children": ext_nodes})
    return {"name": "platform", "value": round(sum(c["value"] for c in children), 1), "children": children}


def sample_field(n: int = 9) -> dict[str, Any]:
    """A generated scalar field on an ``n``³ grid: a smooth bump, for a kind with no real source. SAMPLE."""
    xs = [round(-1 + 2 * i / (n - 1), 4) for i in range(n)]
    values = [round(math.cos(math.pi * x / 2.2) * math.cos(math.pi * y / 2.2) * math.cos(math.pi * z / 2.6), 4)
              for z in xs for y in xs for x in xs]
    return {"grid": {"x": xs, "y": xs, "z": xs, "values": values},
            "scalars": {f"{SAMPLE} value": {"unit": "", "association": "point", "values": values}}}


def resolve_data(params: Mapping[str, Any], *, state_dir: Path, config: KindsConfig | None = None) -> dict[str, Any]:
    """The data for one graph of the group: what :data:`VERB` answers."""
    site = str(params.get("site") or "")
    kind = str(params.get("kind") or "")
    cfg = config or registered(site, state_dir)
    if cfg is None:
        raise KindsError(f"no chart-kinds config is registered for {site!r}; run the kinds-group verb with --config")
    try:
        day = date.fromisoformat(str(params.get("replay") or cfg.replay.isoformat()))
    except ValueError:
        raise KindsError("replay: a day, YYYY-MM-DD") from None
    check_delay(day, cfg.delay_hours)
    return _kind_data(cfg, kind, day, {**params, "_state_dir": str(state_dir)})


def resolver(state_dir: Path):
    """A :data:`~axiom.extensions.builtins.scidisplay.chart_kinds.Resolver` for :data:`VERB`."""

    def _resolve(doc: Mapping[str, Any], data: Mapping[str, Any], state: Any) -> Mapping[str, Any]:
        verb = data.get("verb") or {}
        if verb.get("name") != VERB:
            raise KindsError(f"no figure source answers the verb {verb.get('name')!r} here")
        params = {**(verb.get("params") or {}), **(getattr(state, "params", {}) or {})}
        out = dict(resolve_data(params, state_dir=state_dir))
        if params.get("kind") in REPLAYED:
            _, _, cursor = _cursor(registered(str(params["site"]), state_dir) or _never(), date.fromisoformat(str(params["replay"])), params.get(PLAY_PARAM))
            whole = _frame_index(registered(str(params["site"]), state_dir), params.get(PLAY_PARAM)) is None
            moment = "the whole day" if whole else f"at {cursor.strftime('%H:%M')} UTC"
            out.setdefault("notes", []).append(f"replay of {params['replay']}, {moment}")
        return out

    return _resolve


def _never() -> KindsConfig:
    raise KindsError("no config")


__all__ = [
    "KindsConfig",
    "KindsError",
    "ORDER",
    "VERB",
    "build_group",
    "check_delay",
    "clear_cache",
    "merge_into",
    "platform_timing",
    "register",
    "registered",
    "resolve_data",
    "resolver",
    "sample_field",
]
