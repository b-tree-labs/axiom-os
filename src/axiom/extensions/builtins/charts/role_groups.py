# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Role groups: a product's chart template, filled by one site's map from roles to its channels.

A **template** names roles, never channels: "draw every channel that plays role X on a log
axis", "the total of role Y over the day". A product ships its templates in an extension's
``charts/templates/*.toml``; nothing here knows what any role means. A **site config** says
which of its channels play each role, where its gold is, its time zone, who sees the group
(``kind``, ``visibility``), and how old a day must be before it is shown. Registering the
config makes the pair a prebuilt group of the node (``prebuilt.PROVIDERS``).

**A day picker.** The group carries ``day_picker``, the day it opens on and the days that have
readings (``days``). Every chart's spec names the day in ``data.verb.params.day``; choosing
another day changes that one parameter in every chart, and each is read again from gold. A day
is the site's own calendar day, in its time zone, never the server's.

**Only gold.** Every read goes through :mod:`.kinds_sources` (a gold export, a gold snapshot, a
data kit's snapshots, or a node's data door), none of which can be pointed at silver.

Recipes a template chart may name (``recipe``, default ``series``):

``series``        every channel of ``roles`` over the day (``estimates`` adds a modelled
                  counterpart, dashed and labelled); ``y`` names the value axis type.
``total``         the time integral of the first channel of ``roles`` over the day, in
                  unit-hours, gaps longer than ``max_gap_s`` left out; ``scale`` and ``unit``
                  state the drawn unit.
``peak``          the day's largest reading of that channel, and when.
``state``         that channel bucketed into named ``states`` (``below`` thresholds, lowest first).
``at_peak``       each channel of ``roles`` at the moment the first channel of role ``of`` peaked.
``median_where``  the median over the day of the mean across ``roles``' channels, kept only
                  while role ``where.role`` is ``between`` two values, within ``hours`` of the
                  site's day, with an optional ``spread_below`` across the channels and
                  ``all_above`` floor; fewer than ``min_points`` such moments is "not on this day".
"""

from __future__ import annotations

import hashlib
import json
import statistics
import threading
import tomllib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .kinds_group import Chan, _chan, _hatches
from .kinds_sources import GoldSource, Reading, SourceError, open_source

#: The verb every chart of a role group names; :func:`resolve_data` answers it.
VERB = "charts.role_data"
#: Where a registered site config is kept, under the state directory.
CONFIG_DIR = ("charts", "roles")
#: Where a product extension keeps its templates, under the extension's own folder.
TEMPLATE_DIR = ("charts", "templates")
RECIPES = ("series", "total", "peak", "state", "at_peak", "median_where")
KINDS_OF = {"series": ("line", "step", "area"), "total": ("kpi",), "peak": ("kpi",), "state": ("state",),
            "at_peak": ("bar",), "median_where": ("kpi",)}
#: The resolution the day list is counted at.
DAY_SCAN_STEP = 600


class RoleGroupError(ValueError):
    """A template, a site config or a request the group cannot be built from, and why."""


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Templates: what a product ships.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    charts: tuple[dict[str, Any], ...]
    roles: dict[str, str] = field(default_factory=dict)
    day_picker: bool = True
    layout: str = "2"
    #: The role whose first channel decides which days have readings.
    day_role: str = ""
    path: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, path: str = "") -> Template:
        head = raw.get("template") or {}
        tid = str(head.get("id") or "")
        if not tid:
            raise RoleGroupError(f"{path or 'template'}: [template] id is required")
        charts = [dict(c) for c in raw.get("chart") or []]
        if not charts:
            raise RoleGroupError(f"template {tid}: no [[chart]]")
        from axiom.extensions.builtins.scidisplay.unified_spec import registered_kinds

        seen: set[str] = set()
        for c in charts:
            cid, recipe = str(c.get("id") or ""), str(c.get("recipe") or "series")
            where = f"template {tid}, chart {cid or '?'}"
            if not cid or cid in seen:
                raise RoleGroupError(f"{where}: each chart has its own id")
            seen.add(cid)
            if recipe not in RECIPES:
                raise RoleGroupError(f"{where}: recipe is one of {', '.join(RECIPES)}")
            kind = str(c.get("kind") or KINDS_OF[recipe][0])
            if kind not in registered_kinds() or kind not in KINDS_OF[recipe]:
                raise RoleGroupError(f"{where}: a {recipe} chart is drawn as {' or '.join(KINDS_OF[recipe])}")
            if not c.get("roles"):
                raise RoleGroupError(f"{where}: roles, the roles it draws")
        day_role = str(head.get("day_role") or charts[0]["roles"][0])
        return cls(id=tid, name=str(head.get("name") or tid), charts=tuple(charts),
                   roles={str(k): str(v) for k, v in (head.get("roles") or {}).items()},
                   day_picker=bool(head.get("day_picker", True)), layout=str(head.get("layout") or "2"),
                   day_role=day_role, path=path)

    @classmethod
    def load(cls, path: str | Path) -> Template:
        p = Path(path).expanduser()
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise RoleGroupError(f"{p}: {exc}") from exc
        return cls.from_mapping(raw, path=str(p))


def template_dirs() -> list[Path]:
    """Every extension directory, in discovery order: where a product's templates are found."""
    from axiom.extensions.discovery import get_extension_dirs

    return list(get_extension_dirs())


def templates() -> dict[str, Path]:
    """Every template a product ships, by id (the first found wins, as for extensions)."""
    out: dict[str, Path] = {}
    for root in template_dirs():
        for path in sorted(Path(root).glob("*/" + "/".join(TEMPLATE_DIR) + "/*.toml")):
            try:
                tid = str((tomllib.loads(path.read_text(encoding="utf-8")).get("template") or {}).get("id") or "")
            except (OSError, tomllib.TOMLDecodeError):
                continue
            if tid and tid not in out:
                out[tid] = path
    return out


def find_template(ref: str, *, base: Path | None = None) -> Template:
    """A template by id (from the installed products) or by path."""
    p = Path(ref).expanduser()
    if not p.is_absolute() and base is not None:
        p = base / p
    if ref.endswith(".toml") and p.is_file():
        return Template.load(p)
    found = templates().get(ref)
    if found is None:
        raise RoleGroupError(f"no installed product ships a chart template {ref!r}")
    return Template.load(found)


# ---------------------------------------------------------------------------
# The site's config: roles to channels.
# ---------------------------------------------------------------------------


@dataclass
class RoleGroupConfig:
    template: str
    site: str
    label: str
    roles: dict[str, list[Chan]]
    sources: dict[str, dict[str, Any]]
    timezone: str = "UTC"
    kind: str = "site"
    visibility: str = "shown"
    day: date | None = None
    delay_hours: float = 24.0
    days_back: int = 60
    base: Path = field(default_factory=Path.cwd)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, base: Path | None = None) -> RoleGroupConfig:
        head = raw.get("role_group") or {}
        site, template = str(head.get("site") or ""), str(head.get("template") or "")
        if not (site and template):
            raise RoleGroupError("role_group.site and role_group.template are required")
        sources = {str(k): dict(v) for k, v in (raw.get("sources") or {}).items()}
        if "day" not in sources:
            raise RoleGroupError("sources.day: where the site's gold is read from")
        tz = str(head.get("timezone") or "UTC")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise RoleGroupError(f"role_group.timezone: {tz!r} is not a time zone") from None
        roles = {str(r): [_chan(c, f"roles.{r}") for c in (chans if isinstance(chans, list) else [chans])]
                 for r, chans in (raw.get("roles") or {}).items()}
        try:
            day = date.fromisoformat(str(head["day"])) if head.get("day") else None
        except ValueError:
            raise RoleGroupError("role_group.day: YYYY-MM-DD") from None
        return cls(template=template, site=site, label=str(head.get("label") or site), roles=roles,
                   sources=sources, timezone=tz, kind=str(head.get("kind") or "site"),
                   visibility=str(head.get("visibility") or "shown"), day=day,
                   delay_hours=float(head.get("delay_hours", 24)), days_back=int(head.get("days_back", 60)),
                   base=base or Path.cwd())

    @classmethod
    def load(cls, path: str | Path) -> RoleGroupConfig:
        p = Path(path).expanduser().resolve()
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise RoleGroupError(f"{p}: {exc}") from exc
        return cls.from_mapping(raw, base=p.parent)

    def as_mapping(self) -> dict[str, Any]:
        """The config with relative paths made absolute: what is registered."""

        def _abs(spec: Mapping[str, Any]) -> dict[str, Any]:
            out = dict(spec)
            for key in ("export", "snapshot", "kit"):
                if out.get(key):
                    p = Path(str(out[key])).expanduser()
                    out[key] = str(p if p.is_absolute() else (self.base / p).resolve())
            return out

        template = self.template
        if template.endswith(".toml") and not Path(template).is_absolute():
            template = str((self.base / template).resolve())
        head: dict[str, Any] = {"template": template, "site": self.site, "label": self.label,
                                "timezone": self.timezone, "kind": self.kind, "visibility": self.visibility,
                                "delay_hours": self.delay_hours, "days_back": self.days_back}
        if self.day:
            head["day"] = self.day.isoformat()
        roles = {r: [{k: v for k, v in c.__dict__.items() if v not in ("", None, 1.0) or k in ("ref", "label")}
                     for c in chans] for r, chans in self.roles.items()}
        return {"role_group": head, "sources": {k: _abs(v) for k, v in self.sources.items()}, "roles": roles}

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.as_mapping(), sort_keys=True).encode()).hexdigest()[:12]

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _config_path(state_dir: Path, site: str, template: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in f"{site}--{Path(template).stem}")
    return Path(state_dir).joinpath(*CONFIG_DIR, f"{safe}.json")


def register(cfg: RoleGroupConfig, state_dir: Path) -> Path:
    path = _config_path(state_dir, cfg.site, cfg.template)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.as_mapping(), indent=2, sort_keys=True), encoding="utf-8")
    return path


def registered_all(state_dir: Path) -> list[RoleGroupConfig]:
    root = Path(state_dir).joinpath(*CONFIG_DIR)
    out: list[RoleGroupConfig] = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            out.append(RoleGroupConfig.from_mapping(json.loads(path.read_text(encoding="utf-8")), base=path.parent))
        except (OSError, ValueError):
            continue  # one unreadable registration does not hide the others
    return out


def registered(site: str, template: str, state_dir: Path) -> RoleGroupConfig | None:
    for cfg in registered_all(state_dir):
        if cfg.site == site and (cfg.template == template or Path(cfg.template).stem == template
                                 or _template_of(cfg).id == template):
            return cfg
    return None


# ---------------------------------------------------------------------------
# Days.
# ---------------------------------------------------------------------------


def day_window(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The site's calendar day as an instant range (a short or long day across a clock change)."""
    return (datetime.combine(day, time(), tzinfo=tz).astimezone(UTC),
            datetime.combine(day + timedelta(days=1), time(), tzinfo=tz).astimezone(UTC))


def _old_enough(cfg: RoleGroupConfig, day: date, now: datetime) -> bool:
    return day_window(day, cfg.zone())[1] + timedelta(hours=cfg.delay_hours) <= now


def check_delay(cfg: RoleGroupConfig, day: date) -> None:
    if not _old_enough(cfg, day, _now()):
        raise RoleGroupError(f"{day.isoformat()} is not {cfg.delay_hours:g} h old yet; the group waits that long")


_lock = threading.Lock()
_sources: dict[tuple[str, str], GoldSource] = {}
_days: dict[tuple[str, str], list[dict[str, Any]]] = {}
_templates: dict[str, Template] = {}


def clear_cache() -> None:
    with _lock:
        _sources.clear()
        _days.clear()
        _templates.clear()


def _template_of(cfg: RoleGroupConfig) -> Template:
    with _lock:
        got = _templates.get(cfg.template)
    if got is None:
        got = find_template(cfg.template, base=cfg.base)
        with _lock:
            _templates[cfg.template] = got
    return got


def _source(cfg: RoleGroupConfig) -> GoldSource:
    key = (cfg.digest(), "day")
    with _lock:
        got = _sources.get(key)
    if got is None:
        try:
            got = open_source(cfg.sources["day"], cfg.site, base=cfg.base)
        except SourceError as exc:
            raise RoleGroupError(str(exc)) from exc
        with _lock:
            _sources[key] = got
    return got


def available_days(cfg: RoleGroupConfig, template: Template) -> list[dict[str, Any]]:
    """The site's days with readings on the template's day role, oldest first, old enough to show."""
    now = _now()
    key = (cfg.digest(), now.strftime("%Y-%m-%dT%H"))
    with _lock:
        if key in _days:
            return _days[key]
    chans = cfg.roles.get(template.day_role) or []
    out: list[dict[str, Any]] = []
    if chans:
        tz = cfg.zone()
        today = now.astimezone(tz).date()
        start = day_window(today - timedelta(days=cfg.days_back), tz)[0]
        end = day_window(today, tz)[1]
        try:
            got = _source(cfg).read(chans[0].feed, chans[0].channel, start, end, DAY_SCAN_STEP)
        except (SourceError, OSError) as exc:
            raise RoleGroupError(f"the days with readings cannot be listed: {exc}") from exc
        counts = Counter(datetime.fromtimestamp(t, UTC).astimezone(tz).date() for t, v, _ in got if v is not None)
        out = [{"date": d.isoformat(), "rows": n} for d, n in sorted(counts.items()) if _old_enough(cfg, d, now)]
    with _lock:
        _days[key] = out
    return out


# ---------------------------------------------------------------------------
# The group.
# ---------------------------------------------------------------------------


def _mapped(cfg: RoleGroupConfig, chart: Mapping[str, Any]) -> bool:
    needed = list(chart.get("roles") or []) + ([chart["of"]] if chart.get("of") else [])
    if isinstance(chart.get("where"), Mapping):
        needed.append(chart["where"].get("role"))
    return all(cfg.roles.get(str(r)) for r in needed)


def _spec(cfg: RoleGroupConfig, template: Template, chart: Mapping[str, Any], order: int, day: date,
          group_name: str) -> dict[str, Any]:
    from axiom.extensions.builtins.scidisplay.unified_spec import SCHEMA_VERSION

    recipe = str(chart.get("recipe") or "series")
    kind = str(chart.get("kind") or KINDS_OF[recipe][0])
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": f"role-{template.id}-{cfg.site}-{chart['id']}".lower(),
        "kind": kind,
        "title": str(chart.get("title") or chart["id"]),
        "data": {"verb": {"name": VERB, "params": {"site": cfg.site, "template": template.id,
                                                    "chart": str(chart["id"]), "day": day.isoformat()}}},
    }
    sub = str(chart.get("subtitle") or "")
    if recipe == "state":
        # The band names each state with its word and glyph (scidisplay's state kind).
        doc["encoding"] = {"states": [str(s["name"]) for s in chart.get("states") or []]}
    if sub:
        doc["subtitle"] = sub
    if chart.get("y"):
        doc["axes"] = {"y": {"type": str(chart["y"])}}
    estimates = bool(chart.get("estimates"))
    try:
        origin = _source(cfg).lineage()
    except RoleGroupError as exc:
        origin = f"unavailable: {exc}"
    doc["catalog"] = {"group": group_name, "order": order, "origin": origin, "verb": VERB,
                      "provenance": "hybrid" if estimates else "measured",
                      "roles": [str(r) for r in [*(chart.get("roles") or []), *(chart.get("estimates") or [])]]}
    return doc


def build_group(cfg: RoleGroupConfig, *, day: date | None = None) -> dict[str, Any]:
    """The prebuilt group: one graph per template chart the site maps, a day picker, and the days."""
    template = _template_of(cfg)
    days = available_days(cfg, template) if template.day_picker else []
    if day is None:
        day = cfg.day or (date.fromisoformat(days[-1]["date"]) if days else None)
    if day is None:
        tz = cfg.zone()
        day = _now().astimezone(tz).date() - timedelta(days=1 + int(cfg.delay_hours // 24))
    name = f"{template.name} · {cfg.label}"
    graphs, omitted = [], []
    for order, chart in enumerate(template.charts):
        if not _mapped(cfg, chart):
            omitted.append({"chart": str(chart["id"]), "roles": [str(r) for r in chart.get("roles") or []]})
            continue
        spec = _spec(cfg, template, chart, order, day, name)
        graphs.append({"id": f"g{len(graphs) + 1}", "site": cfg.site, "feeds": [], "series": [spec["id"]],
                       "name": spec["title"], "kind": spec["kind"], "spec": spec, "drawer": False})
    if not graphs:
        raise RoleGroupError(f"{cfg.site} maps none of the roles template {template.id} draws")
    return {"id": f"{template.id}-{cfg.site}", "name": name, "graphs": graphs, "layout": template.layout,
            "syncDates": False, "syncRun": False, "last": "", "window": None, "expanded": None,
            "day_picker": template.day_picker, "day": day.isoformat(), "days": days,
            "kind": cfg.kind, "visibility": cfg.visibility, "omitted": omitted}


def provider(state_dir: Path) -> list[dict[str, Any]]:
    """Every registered role group: a provider of prebuilt groups (``prebuilt.PROVIDERS``)."""
    import logging

    out = []
    for cfg in registered_all(state_dir):
        try:
            out.append(build_group(cfg))
        except RoleGroupError as exc:
            logging.getLogger("axi.charts.roles").warning("role group %s for %s not offered: %s",
                                                          cfg.template, cfg.site, exc)
    return out


# ---------------------------------------------------------------------------
# Answering a chart's data at draw time.
# ---------------------------------------------------------------------------


def _iso(t: float, cfg: RoleGroupConfig) -> str:
    """An instant in the site's own offset, so a time axis reads the site's clock."""
    return datetime.fromtimestamp(t, UTC).astimezone(cfg.zone()).isoformat()


def _read(cfg: RoleGroupConfig, ch: Chan, start: datetime, end: datetime, step: int) -> list[Reading]:
    try:
        got = _source(cfg).read(ch.feed, ch.channel, start, end, step)
    except (SourceError, OSError) as exc:
        raise RoleGroupError(str(exc)) from exc
    if ch.valid_above is not None:
        got = [(t, None, "bad") if v is not None and v <= ch.valid_above else (t, v, q) for t, v, q in got]
    if ch.scale != 1.0:
        got = [(t, None if v is None else v * ch.scale, q) for t, v, q in got]
    return got


def _unit(cfg: RoleGroupConfig, ch: Chan) -> str:
    return ch.unit or _source(cfg).unit(ch.feed, ch.channel)


def _first(cfg: RoleGroupConfig, role: str) -> Chan:
    chans = cfg.roles.get(role) or []
    if not chans:
        raise RoleGroupError(f"{cfg.site} maps no channel to the role {role!r}")
    return chans[0]


def _series(cfg: RoleGroupConfig, chart: Mapping[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    step = int(chart.get("step_s") or 60)
    series, marks = [], []
    measured: list[str] = []
    for role, simulated in [*((r, False) for r in chart.get("roles") or []),
                            *((r, True) for r in chart.get("estimates") or [])]:
        for ch in cfg.roles.get(str(role)) or []:
            got = _read(cfg, ch, start, end, step)
            s: dict[str, Any] = {"name": ch.label, "unit": _unit(cfg, ch), "x": [_iso(t, cfg) for t, _, _ in got],
                                 "y": [None if v is None else round(v, 4) for _, v, _ in got]}
            if simulated or ch.role == "simulated":
                s["role"] = "simulated"
                twin = ch.counterpart_of or (measured[0] if measured else "")
                if twin:
                    s["counterpart_of"] = twin
            else:
                measured.append(ch.label)
            if ch.sigma:
                sig = {round(t): v for t, v, _ in _read(cfg, replace(_chan(ch.sigma, "sigma"), scale=ch.scale),
                                                        start, end, step)}
                s["low"] = [None if v is None or sig.get(round(t)) is None else round(v - sig[round(t)], 4)
                            for t, v, _ in got]
                s["high"] = [None if v is None or sig.get(round(t)) is None else round(v + sig[round(t)], 4)
                             for t, v, _ in got]
            series.append(s)
            marks.extend(_hatches(got, ch.label, step))
    if not any(v is not None for s in series for v in s["y"]):
        raise RoleGroupError("no readings on this day")
    out: dict[str, Any] = {"series": series, "x_range": _day_range(start, end, cfg)}
    if marks:
        out["marks"] = marks
    return out


def _day_range(start: datetime, end: datetime, cfg: RoleGroupConfig) -> list[str]:
    """The day's two edges, which the time axis spans however late the readings begin (as the
    legacy day figure does): the renderer's ``x_range``."""
    return [_iso(start.timestamp(), cfg), _iso(end.timestamp(), cfg)]


def _total(cfg: RoleGroupConfig, chart: Mapping[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    ch = _first(cfg, str(chart["roles"][0]))
    step = int(chart.get("step_s") or 60)
    gap = float(chart.get("max_gap_s") or 3600)
    pts = [(t, v) for t, v, _ in _read(cfg, ch, start, end, step) if v is not None]
    total = sum((a[1] + b[1]) / 2.0 * (b[0] - a[0]) / 3600.0 for a, b in zip(pts, pts[1:], strict=False)
                if 0 < b[0] - a[0] <= gap)
    unit = str(chart.get("unit") or f"{_unit(cfg, ch)} h")
    value = round(total * float(chart.get("scale") or 1.0), 4) if pts else None
    return {"series": [{"name": str(chart.get("label") or chart.get("title") or ch.label), "unit": unit, "y": [value]}]}


def _peak(cfg: RoleGroupConfig, chart: Mapping[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    ch = _first(cfg, str(chart["roles"][0]))
    got = [(t, v) for t, v, _ in _read(cfg, ch, start, end, int(chart.get("step_s") or 60)) if v is not None]
    if not got:
        raise RoleGroupError("no readings on this day")
    t, v = max(got, key=lambda p: (p[1], -p[0]))
    scale = float(chart.get("scale") or 1.0)
    when = datetime.fromtimestamp(t, UTC).astimezone(cfg.zone()).strftime("%H:%M")
    return {"series": [{"name": f"{chart.get('label') or ch.label} at {when}",
                        "unit": str(chart.get("unit") or _unit(cfg, ch)), "y": [round(v * scale, 4)]}]}


def _state(cfg: RoleGroupConfig, chart: Mapping[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    ch = _first(cfg, str(chart["roles"][0]))
    states = [(str(s["name"]), float(s["below"]) if s.get("below") is not None else None)
              for s in chart.get("states") or []]
    if not states:
        raise RoleGroupError("states: [{name, below}, ..., {name}] from lowest to highest")

    def _of(v: float | None) -> int | None:
        if v is None:
            return None
        return next((i for i, (_, below) in enumerate(states) if below is None or v < below), len(states) - 1)

    got = _read(cfg, ch, start, end, int(chart.get("step_s") or 60))
    return {"series": [{"name": "State", "unit": "", "levels": [name for name, _ in states],
                        "x": [_iso(t, cfg) for t, _, _ in got],
                        "y": [_of(v) for _, v, _ in got]}], "x_range": _day_range(start, end, cfg)}


def _at_peak(cfg: RoleGroupConfig, chart: Mapping[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    step = int(chart.get("step_s") or 60)
    lead = [(t, v) for t, v, _ in _read(cfg, _first(cfg, str(chart["of"])), start, end, step) if v is not None]
    if not lead:
        raise RoleGroupError("no readings on this day")
    at = round(max(lead, key=lambda p: (p[1], -p[0]))[0])
    names, values, unit = [], [], ""
    for role in chart.get("roles") or []:
        for ch in cfg.roles.get(str(role)) or []:
            row = {round(t): v for t, v, _ in _read(cfg, ch, start, end, step)}
            names.append(ch.label)
            values.append(None if row.get(at) is None else round(row[at], 4))
            unit = unit or _unit(cfg, ch)
    when = datetime.fromtimestamp(at, UTC).astimezone(cfg.zone()).strftime("%H:%M")
    return {"series": [{"name": str(chart.get("label") or chart.get("title") or "At peak"), "unit": unit,
                        "x": names, "y": values}], "notes": [f"read at {when}, the peak"]}


def _median_where(cfg: RoleGroupConfig, chart: Mapping[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    step = int(chart.get("step_s") or 60)
    where = chart.get("where") or {}
    lo, hi = (float(x) for x in where.get("between") or (float("-inf"), float("inf")))
    cond = {round(t) for t, v, _ in _read(cfg, _first(cfg, str(where.get("role"))), start, end, step)
            if v is not None and lo <= v <= hi}
    h0, h1 = (float(x) for x in chart.get("hours") or (0, 24))
    tz = cfg.zone()
    cols, unit = [], ""
    for role in chart.get("roles") or []:
        for ch in cfg.roles.get(str(role)) or []:
            cols.append({round(t): v for t, v, _ in _read(cfg, ch, start, end, step) if v is not None})
            unit = unit or _unit(cfg, ch)
    spread_below, floor = chart.get("spread_below"), chart.get("all_above")
    kept: list[float] = []
    for t in sorted(cond):
        local = datetime.fromtimestamp(t, UTC).astimezone(tz)
        if not (h0 <= local.hour + local.minute / 60.0 < h1):
            continue
        vals = [c[t] for c in cols if t in c]
        if len(vals) != len(cols) or not vals:
            continue
        if spread_below is not None and max(vals) - min(vals) >= float(spread_below):
            continue
        if floor is not None and min(vals) <= float(floor):
            continue
        kept.append(statistics.fmean(vals))
    name = str(chart.get("label") or chart.get("title") or "Median")
    if len(kept) < int(chart.get("min_points") or 1):
        # Said as a sentence, drawn as nothing else: an empty tile or axis reads as a value.
        raise RoleGroupError("not measured on this day: the condition never held long enough")
    return {"series": [{"name": name, "unit": unit, "y": [round(statistics.median(kept), 2)]}],
            "notes": [f"median of {len(kept)} moments"]}


_RECIPE = {"series": _series, "total": _total, "peak": _peak, "state": _state, "at_peak": _at_peak,
           "median_where": _median_where}


def resolve_data(params: Mapping[str, Any], *, state_dir: Path, config: RoleGroupConfig | None = None) -> dict[str, Any]:
    """The data for one chart of a role group, for the day its params name: what :data:`VERB` answers."""
    site, template_id, chart_id = (str(params.get(k) or "") for k in ("site", "template", "chart"))
    cfg = config or registered(site, template_id, state_dir)
    if cfg is None:
        raise RoleGroupError(f"no role group {template_id!r} is registered for {site!r}; run role-group with --config")
    template = _template_of(cfg)
    chart = next((c for c in template.charts if str(c["id"]) == chart_id), None)
    if chart is None:
        raise RoleGroupError(f"template {template.id} has no chart {chart_id!r}")
    try:
        day = date.fromisoformat(str(params.get("day") or ""))
    except ValueError:
        raise RoleGroupError("day: YYYY-MM-DD") from None
    check_delay(cfg, day)
    start, end = day_window(day, cfg.zone())
    out = _RECIPE[str(chart.get("recipe") or "series")](cfg, chart, start, end)
    out.setdefault("notes", []).append(f"{day.isoformat()}, {cfg.label}'s day ({cfg.timezone})")
    return out


def resolver(state_dir: Path):
    """A figure-source resolver for :data:`VERB`."""

    def _resolve(doc: Mapping[str, Any], data: Mapping[str, Any], state: Any) -> Mapping[str, Any]:
        verb = data.get("verb") or {}
        if verb.get("name") != VERB:
            raise RoleGroupError(f"no figure source answers the verb {verb.get('name')!r} here")
        return resolve_data({**(verb.get("params") or {}), **(getattr(state, "params", {}) or {})},
                            state_dir=state_dir)

    return _resolve


__all__ = [
    "RECIPES",
    "VERB",
    "RoleGroupConfig",
    "RoleGroupError",
    "Template",
    "available_days",
    "build_group",
    "check_delay",
    "clear_cache",
    "day_window",
    "find_template",
    "provider",
    "register",
    "registered",
    "registered_all",
    "resolve_data",
    "resolver",
    "templates",
]
