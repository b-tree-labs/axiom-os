# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The disk-pressure ladder: margin in bytes to the line that actually evicts.

Percent used is the wrong measure for a node running a kubelet. What takes the
node down is the kubelet's hard eviction threshold (``evictionHard``, for
example ``nodefs.available < 50Gi``): below it, pods are evicted, starting with
whatever uses the most disk, which on a data node is the database. And once
pressure is on, the kubelet keeps it on until free space reaches the floor
**plus** ``evictionMinimumReclaim``. A reclaim of 10 % on a 3.3 TB volume is
330 GB, so freeing files does nothing visible for a long time. Image garbage
collection runs under the same pressure and deletes images it thinks it can
pull back, which an image with ``imagePullPolicy: Never`` cannot be.

So, for each volume the node depends on, this module computes:

- ``floor``: the strictest eviction threshold of the signals that govern it
  (``nodefs``, ``imagefs``), read from the kubelet's ``configz``; zero when no
  kubelet governs the volume.
- ``recovery_line``: floor + minimum reclaim, where pressure clears.
- ``margin``: free − floor, or free − recovery_line while pressure is on.

and places it on a ladder (all thresholds per site):

=========  ===================================================
warn       margin < 150 GB, or free < recovery_line
page       margin < 80 GB, or a never-pull image is missing
critical   free < floor + 20 GB, or pressure is on
=========  ===================================================

Everything here is pure: the probes (``df``, ``kubectl``, the container
runtime) arrive through a :class:`Probes` object and notifications through a
callable, so the ladder is tested without a cluster. The probes that read a
real node live in :mod:`.disk_probes`.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

GB = 10**9

#: Ladder rungs, mildest first. ``unknown`` is not a rung: it means a reading
#: could not be taken, and it never outranks a measured rung.
RUNGS = ("ok", "warn", "page", "critical")
_RANK = {r: i for i, r in enumerate(RUNGS)}

#: A volume whose path could not be resolved. Not a rung of its own: it lifts
#: the report to at least ``warn`` so a person hears that the ladder is blind
#: to a volume it was told to watch.
UNRESOLVED = "unresolved"

#: The notifications priority each rung is sent at.
PRIORITY = {"ok": "low", "warn": "normal", "page": "high", "critical": "urgent"}

#: Disk signals the kubelet evicts on. Memory and PID signals are not disk.
DISK_SIGNALS = ("nodefs", "imagefs", "containerfs")

#: A saved reading older than this is reported as stale by the status line.
STALE_AFTER = timedelta(minutes=15)

_UNITS = {
    "": 1,
    "k": 10**3, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50,
}
_QTY = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kKMGTP]i?|)\s*$")


def parse_quantity(text: Any, total_bytes: int | None = None) -> int | None:
    """A Kubernetes quantity (``50Gi``, ``1G``, ``10%``) in bytes, or None.

    A percentage is of the volume the signal governs, so it needs that
    volume's size; without one it cannot be resolved and None says so.
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    if s.endswith("%"):
        try:
            pct = float(s[:-1])
        except ValueError:
            return None
        if total_bytes is None:
            return None
        return int(total_bytes * pct / 100)
    m = _QTY.match(s)
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def _gb(n: int | float | None) -> float | None:
    return None if n is None else round(n / GB, 2)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LadderThresholds:
    warn_margin_gb: float = 150
    page_margin_gb: float = 80
    critical_over_floor_gb: float = 20
    #: How often a standing page or critical is repeated.
    repeat_minutes: int = 60
    #: How often a standing warning is repeated.
    warn_repeat_hours: int = 24

    def validate(self) -> None:
        if not self.page_margin_gb < self.warn_margin_gb:
            raise ValueError(
                f"page_margin_gb ({self.page_margin_gb}) must be below "
                f"warn_margin_gb ({self.warn_margin_gb})"
            )
        if self.critical_over_floor_gb < 0:
            raise ValueError("critical_over_floor_gb must not be negative")


@dataclass(frozen=True)
class VolumeSpec:
    """A volume the node depends on, and the kubelet signals that govern it.

    On a k3d node the kubelet's ``nodefs`` and ``imagefs`` both live on the
    container runtime's data root, so that volume carries both signals. A
    volume outside the runtime (a bronze archive, a bind-mounted database
    volume) carries none and is measured from zero.
    """

    name: str
    path: str
    signals: tuple[str, ...] = ()
    #: Why ``path`` could not be resolved (an ``auto:`` path nothing on this node
    #: could answer). Non-empty means: report ``unresolved`` and alert, and never
    #: measure some other volume in its place.
    unresolved: str = ""


@dataclass
class LadderConfig:
    volumes: list[VolumeSpec] = field(default_factory=list)
    thresholds: LadderThresholds = field(default_factory=LadderThresholds)
    #: Who is told. Empty: the probes' default (the site operator).
    recipient: str = ""
    #: The k3d cluster name, used in the re-import remedy.
    cluster: str = ""
    kubectl_context: str = ""
    #: Images to check even when no pod lists them (e.g. a scaled-down database).
    images: list[str] = field(default_factory=list)
    source: str = "default"


def load_config(path: str | Path) -> LadderConfig:
    """Read a site's ``disk-ladder.toml``. Raises ``ValueError`` on a bad ladder."""
    p = Path(path)
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    ladder = data.get("ladder", {}) or {}
    defaults = LadderThresholds()
    thresholds = LadderThresholds(
        warn_margin_gb=float(ladder.get("warn_margin_gb", defaults.warn_margin_gb)),
        page_margin_gb=float(ladder.get("page_margin_gb", defaults.page_margin_gb)),
        critical_over_floor_gb=float(
            ladder.get("critical_over_floor_gb", defaults.critical_over_floor_gb)
        ),
        repeat_minutes=int(ladder.get("repeat_minutes", defaults.repeat_minutes)),
        warn_repeat_hours=int(ladder.get("warn_repeat_hours", defaults.warn_repeat_hours)),
    )
    thresholds.validate()
    kubelet = data.get("kubelet", {}) or {}
    volumes = []
    for v in data.get("volume", []) or []:
        if not v.get("name") or not v.get("path"):
            raise ValueError(f"every [[volume]] needs a name and a path: {v!r}")
        signals = tuple(str(s) for s in (v.get("signals") or ()))
        unknown = [s for s in signals if s not in DISK_SIGNALS]
        if unknown:
            raise ValueError(f"volume {v['name']!r}: unknown signal(s) {unknown}; "
                             f"choose from {list(DISK_SIGNALS)}")
        path = str(v["path"])
        if not path.startswith("auto:"):
            path = str(Path(path).expanduser())
        volumes.append(VolumeSpec(name=str(v["name"]), path=path, signals=signals))
    return LadderConfig(
        volumes=volumes,
        thresholds=thresholds,
        recipient=str(ladder.get("recipient", "") or ""),
        cluster=str(kubelet.get("cluster", "") or ""),
        kubectl_context=str(kubelet.get("kubectl_context", "") or ""),
        images=[str(i) for i in (kubelet.get("images") or [])],
        source=str(p),
    )


# --------------------------------------------------------------------------
# The kubelet's policy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvictionPolicy:
    hard: dict[str, str] = field(default_factory=dict)
    minimum_reclaim: dict[str, str] = field(default_factory=dict)
    #: ``configz`` (read from the kubelet), ``none`` (no kubelet), or
    #: ``unread`` (a kubelet exists but its thresholds could not be read).
    source: str = "none"

    @classmethod
    def none(cls) -> EvictionPolicy:
        return cls(source="none")

    @classmethod
    def unread(cls) -> EvictionPolicy:
        return cls(source="unread")

    @classmethod
    def from_configz(cls, payload: dict[str, Any]) -> EvictionPolicy:
        cfg = payload.get("kubeletconfig", payload) or {}
        return cls(
            hard={str(k): str(v) for k, v in (cfg.get("evictionHard") or {}).items()},
            minimum_reclaim={
                str(k): str(v) for k, v in (cfg.get("evictionMinimumReclaim") or {}).items()
            },
            source="configz",
        )

    def _max_over(self, table: dict[str, str], signals: Iterable[str], total: int | None) -> int:
        best = 0
        for sig in signals:
            q = parse_quantity(table.get(f"{sig}.available"), total)
            if q is not None and q > best:
                best = q
        return best

    def floor_bytes(self, signals: Sequence[str], total: int | None) -> int:
        """The strictest hard threshold among ``signals``; 0 when none governs."""
        return self._max_over(self.hard, signals, total)

    def reclaim_bytes(self, signals: Sequence[str], total: int | None) -> int:
        return self._max_over(self.minimum_reclaim, signals, total)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "evictionHard": {k: v for k, v in self.hard.items() if _is_disk(k)},
            "evictionMinimumReclaim": {
                k: v for k, v in self.minimum_reclaim.items() if _is_disk(k)
            },
        }


def _is_disk(key: str) -> bool:
    return key.split(".", 1)[0] in DISK_SIGNALS


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------


@dataclass
class VolumeReading:
    name: str
    path: str
    signals: tuple[str, ...]
    total_bytes: int | None
    free_bytes: int | None
    error: str = ""
    unresolved: str = ""


def _worst(rungs: Iterable[str]) -> str:
    measured = [r for r in rungs if r in _RANK]
    if not measured:
        return "unknown"
    return max(measured, key=lambda r: _RANK[r])


def assess_volume(
    reading: VolumeReading,
    policy: EvictionPolicy,
    thresholds: LadderThresholds,
    pressure_on: bool | None,
) -> dict[str, Any]:
    """One volume's place on the ladder, with the reasons in words."""
    base: dict[str, Any] = {
        "name": reading.name,
        "path": reading.path,
        "signals": list(reading.signals),
    }
    if reading.unresolved:
        return {**base, "rung": UNRESOLVED,
                "reasons": [f"unresolved: {reading.unresolved}; pin this volume's path in "
                            "the site's disk-ladder config"]}
    if reading.free_bytes is None or reading.total_bytes is None:
        return {**base, "rung": "unknown",
                "reasons": [f"could not read {reading.path}: {reading.error or 'no reading'}"]}

    free = reading.free_bytes
    total = reading.total_bytes
    floor = policy.floor_bytes(reading.signals, total)
    reclaim = policy.reclaim_bytes(reading.signals, total)
    recovery = floor + reclaim
    governed = bool(reading.signals) and floor > 0
    # Below the floor the kubelet is evicting whether or not anybody read the
    # node condition. Pressure reported by the node only counts for a volume
    # the kubelet actually watches.
    on = bool(governed and (free < floor or pressure_on))
    effective = recovery if on else floor
    margin = free - effective
    reasons: list[str] = []
    rung = "ok"

    def raise_to(r: str, why: str) -> None:
        nonlocal rung
        reasons.append(why)
        if _RANK[r] > _RANK[rung]:
            rung = r

    t = thresholds
    if margin < t.warn_margin_gb * GB:
        raise_to("warn", f"margin {_gb(margin):.0f} GB < warn {t.warn_margin_gb:.0f} GB")
    if margin < t.page_margin_gb * GB:
        raise_to("page", f"margin {_gb(margin):.0f} GB < page {t.page_margin_gb:.0f} GB")
    if free < floor + t.critical_over_floor_gb * GB:
        raise_to("critical", f"free {_gb(free):.0f} GB < floor {_gb(floor):.0f} GB "
                             f"+ {t.critical_over_floor_gb:.0f} GB")
    if on:
        raise_to("critical", f"disk pressure is on; it stays on until free reaches "
                             f"{_gb(recovery):.0f} GB (floor {_gb(floor):.0f} GB + minimum "
                             f"reclaim {_gb(reclaim):.0f} GB)")
    elif governed and reclaim > 0 and free < recovery:
        raise_to("warn", f"minimum reclaim {_gb(reclaim):.0f} GB: if pressure starts it stays on "
                         f"until free reaches {_gb(recovery):.0f} GB, more than the "
                         f"{_gb(free):.0f} GB free now")

    return {
        **base,
        "rung": rung,
        "reasons": reasons,
        "total_gb": _gb(total),
        "free_gb": _gb(free),
        "used_pct": round(100 * (total - free) / total, 1) if total else None,
        "floor_gb": _gb(floor),
        "minimum_reclaim_gb": _gb(reclaim),
        "recovery_line_gb": _gb(recovery),
        "pressure_on": on,
        "effective_floor_gb": _gb(effective),
        "margin_gb": _gb(margin),
    }


@dataclass(frozen=True)
class ImageCheck:
    """An ``imagePullPolicy: Never`` image: is it still on the node, and on the host?"""

    image: str
    on_node: bool | None
    on_host: bool | None = None


def assess_images(checks: Iterable[ImageCheck], *, cluster: str = "") -> list[dict[str, Any]]:
    out = []
    for c in checks:
        row: dict[str, Any] = {"image": c.image, "on_node": c.on_node, "on_host": c.on_host}
        if c.on_node is True:
            row.update(rung="ok", remedy="")
        elif c.on_node is None:
            row.update(rung="unknown", remedy="could not list the node's images")
        else:
            if c.on_host:
                cflag = f" -c {cluster}" if cluster else ""
                remedy = f"k3d image import {c.image}{cflag}"
            else:
                remedy = (f"rebuild {c.image} on the host, then import it into the node "
                          "(the kubelet cannot pull a never-pull image back)")
            row.update(rung="page", remedy=remedy)
        out.append(row)
    return out


def build_report(
    *,
    volumes: list[dict[str, Any]],
    images: list[dict[str, Any]],
    policy: EvictionPolicy,
    thresholds: LadderThresholds,
    now: datetime,
    node: str = "",
    findings: list[str] | None = None,
) -> dict[str, Any]:
    measured = [v for v in volumes if v.get("margin_gb") is not None]
    unresolved = [v["name"] for v in volumes if v.get("rung") == UNRESOLVED]
    unread = [v["name"] for v in volumes
              if v.get("margin_gb") is None and v.get("rung") != UNRESOLVED]
    # A ladder that read no volume has measured nothing, whatever the images say.
    rung = (_worst([v["rung"] for v in volumes] + [i["rung"] for i in images])
            if measured or not volumes else "unknown")
    findings = list(findings or [])
    if unresolved:
        # Blind to a volume it was told to watch: that is worth a person's attention.
        rung = _worst([rung, "warn"])
        findings.append(f"unresolved: {', '.join(unresolved)}; nothing on this node said where "
                        "it is, so it was not measured (pin its path in the site config)")
    if unread:
        findings.append(f"could not read {', '.join(unread)}; configure the volumes this node "
                        "depends on (a runtime inside a VM keeps its data root out of reach)")
    tightest = min(measured, key=lambda v: v["margin_gb"]) if measured else None
    report = {
        "rung": rung,
        "checked_at": now.isoformat(),
        "node": node,
        "thresholds": {
            "warn_margin_gb": thresholds.warn_margin_gb,
            "page_margin_gb": thresholds.page_margin_gb,
            "critical_over_floor_gb": thresholds.critical_over_floor_gb,
        },
        "kubelet": policy.to_dict(),
        "volumes": volumes,
        "images": images,
        "tightest": tightest["name"] if tightest else "",
        "findings": findings,
    }
    if policy.source == "unread":
        report["findings"].append(
            "a kubelet runs here but its eviction thresholds could not be read (configz); "
            "margins are measured from zero"
        )
    report["summary"] = status_line(report)
    return report


def status_line(report: dict[str, Any]) -> str:
    """One line: the rung and the tightest volume's margin."""
    rung = report.get("rung", "unknown")
    tight = next((v for v in report.get("volumes", []) if v["name"] == report.get("tightest")),
                 None)
    parts = [rung]
    if tight:
        parts.append(f"{tight['name']} margin {tight['margin_gb']:.0f} GB to eviction "
                     f"({tight['free_gb']:.0f} GB free, floor {tight['floor_gb']:.0f} GB)")
    missing = [i["image"] for i in report.get("images", []) if i.get("on_node") is False]
    if missing:
        parts.append(f"image missing: {', '.join(missing)}")
    blind = [v["name"] for v in report.get("volumes", []) if v.get("rung") == UNRESOLVED]
    if blind:
        parts.append(f"unresolved: {', '.join(blind)}")
    if not tight and not missing and not blind:
        parts.append("no volume could be read")
    return " — ".join(parts)


# --------------------------------------------------------------------------
# Notification
# --------------------------------------------------------------------------


def _parse_ts(s: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def should_notify(
    report: dict[str, Any],
    previous: dict[str, Any] | None,
    thresholds: LadderThresholds,
    now: datetime,
) -> bool:
    """Notify on every change of rung (up or down) and repeat a standing one.

    ``previous`` is the last *notified* state. A first run at ``ok`` is silent;
    ``unknown`` never notifies by itself (the probes say why in the report).
    """
    rung = report["rung"]
    if rung == "unknown":
        return False
    prev_rung = (previous or {}).get("rung") or "ok"
    if rung != prev_rung:
        return True
    if rung == "ok":
        return False
    last = _parse_ts((previous or {}).get("notified_at"))
    if last is None:
        return True
    gap = (timedelta(hours=thresholds.warn_repeat_hours) if rung == "warn"
           else timedelta(minutes=thresholds.repeat_minutes))
    return now - last >= gap


def compose_alert(report: dict[str, Any], previous_rung: str, recipient: str) -> dict[str, Any]:
    rung = report["rung"]
    host = report.get("node") or "this node"
    if rung == "ok":
        summary = f"Disk recovered on {host}: {report['summary']}"
    else:
        summary = f"Disk {rung.upper()} on {host}: {report['summary']}"
    lines = []
    for v in report.get("volumes", []):
        if v["rung"] != "ok":
            lines.append(f"- {v['name']} ({v['path']}): {v['rung']}; " + "; ".join(v["reasons"]))
    for i in report.get("images", []):
        if i["rung"] != "ok":
            lines.append(f"- image {i['image']}: {i['rung']}; {i['remedy']}")
    for f in report.get("findings", []):
        lines.append(f"- {f}")
    if rung != "ok":
        lines.append("Runbook: the disk-pressure ladder (what to do at each rung).")
    return {
        "recipient": recipient,
        "rung": rung,
        "previous_rung": previous_rung,
        "priority": PRIORITY.get(rung, "normal"),
        "summary": summary,
        "body": "\n".join(lines),
        "dedup_key": f"disk-ladder:{host}:{rung}:{report['checked_at']}",
    }


# --------------------------------------------------------------------------
# The watch
# --------------------------------------------------------------------------


@dataclass
class KubeletView:
    policy: EvictionPolicy
    pressure_on: bool | None = None
    images: list[ImageCheck] = field(default_factory=list)
    node: str = ""
    findings: list[str] = field(default_factory=list)


class Probes(Protocol):
    def usage(self, path: str) -> tuple[int, int]:
        """(total_bytes, free_bytes) for the filesystem holding ``path``."""

    def kubelet(self, cfg: LadderConfig) -> KubeletView:
        """The kubelet's eviction policy, pressure condition and never-pull images."""


def assess(cfg: LadderConfig, probes: Probes, now: datetime) -> dict[str, Any]:
    """Read every volume and the kubelet; no state, no notification."""
    view = probes.kubelet(cfg)
    volumes = []
    for spec in cfg.volumes:
        if spec.unresolved:
            reading = VolumeReading(spec.name, spec.path, spec.signals, None, None,
                                    unresolved=spec.unresolved)
            volumes.append(assess_volume(reading, view.policy, cfg.thresholds, view.pressure_on))
            continue
        try:
            total, free = probes.usage(spec.path)
            reading = VolumeReading(spec.name, spec.path, spec.signals, total, free)
        except OSError as exc:
            reading = VolumeReading(spec.name, spec.path, spec.signals, None, None,
                                    error=exc.strerror or str(exc))
        volumes.append(assess_volume(reading, view.policy, cfg.thresholds, view.pressure_on))
    return build_report(
        volumes=volumes,
        images=assess_images(view.images, cluster=cfg.cluster),
        policy=view.policy,
        thresholds=cfg.thresholds,
        now=now,
        node=view.node,
        findings=view.findings,
    )


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def watch(
    cfg: LadderConfig,
    *,
    probes: Probes,
    notify: Callable[[dict[str, Any]], Any],
    state_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assess, notify when the ladder says so, and save the reading for the status line.

    A notification that fails is not recorded as sent, so the next run tries
    again: a monitor that drops its own page silently is no monitor.
    """
    now = now or datetime.now(UTC)
    report = assess(cfg, probes, now)
    saved = _read_state(state_path) or {}
    previous = saved.get("notified")
    report["notified"] = False
    report["notify_errors"] = []
    if should_notify(report, previous, cfg.thresholds, now):
        alert = compose_alert(report, (previous or {}).get("rung") or "ok", cfg.recipient)
        try:
            notify(alert)
        except Exception as exc:  # noqa: BLE001 - reported, and retried next run
            report["notify_errors"].append(f"{type(exc).__name__}: {exc}")
        else:
            report["notified"] = True
            previous = {"rung": report["rung"], "notified_at": now.isoformat()}
    _write_state(state_path, {"last": report, "notified": previous})
    return report


def last_status(state_path: Path, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The last saved reading, for ``status``; None when the ladder never ran."""
    saved = _read_state(state_path)
    if not saved or not isinstance(saved.get("last"), dict):
        return None
    last = saved["last"]
    now = now or datetime.now(UTC)
    at = _parse_ts(last.get("checked_at"))
    return {
        "rung": last.get("rung", "unknown"),
        "summary": last.get("summary", ""),
        "checked_at": last.get("checked_at"),
        "stale": at is None or now - at > STALE_AFTER,
    }


__all__ = [
    "DISK_SIGNALS",
    "PRIORITY",
    "RUNGS",
    "UNRESOLVED",
    "EvictionPolicy",
    "ImageCheck",
    "KubeletView",
    "LadderConfig",
    "LadderThresholds",
    "Probes",
    "VolumeReading",
    "VolumeSpec",
    "assess",
    "assess_images",
    "assess_volume",
    "build_report",
    "compose_alert",
    "last_status",
    "load_config",
    "parse_quantity",
    "should_notify",
    "status_line",
    "watch",
]
