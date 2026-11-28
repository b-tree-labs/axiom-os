# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Configured storage connectors, mounted by name and reloaded at runtime (ADR-220 §2-3).

A connector is declared once, as configuration: a name, a ``kind`` and its
settings. The :class:`StorageConnectorManager` turns that into a live provider
through the factory registered for the kind, and every caller asks for the
connector by NAME (:meth:`~StorageConnectorManager.lease`), never by vendor.

Reload semantics, stated once:

* **add**: built and probed; it resolves only once its probe passes;
* **change**: a new instance is built BESIDE the old one and probed; the swap is
  atomic and happens only on a passing probe. A failed build or probe leaves the
  old instance serving and the connector ``degraded`` with the reason;
* **remove**: the name stops resolving for new work; the instance is closed when
  its last lease is released;
* **in-flight** work holds a lease on the instance it started on and finishes on
  it, so a reload never interrupts a transfer;
* **isolation**: each connector reloads on its own. One bad file, factory or
  probe affects only its connector, and nothing restarts.

Triggers: :meth:`~StorageConnectorManager.reload` (what ``axi connector reload``
calls), :func:`install_reload_signal` (``SIGHUP`` in a long-running service), and
the reload stamp: ``axi connector reload`` in another process touches it, and a
manager re-reads its configuration the next time it is asked for a connector
after the stamp moved (checked at most every :data:`STAMP_CHECK_S` seconds, so
there is no watcher thread and no polling loop).
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import registry
from .roles import ABSENT, DEFAULT_CONNECTOR, declared_role, declared_roles, load_role_map

log = logging.getLogger(__name__)

#: How often a manager looks at the reload stamp, at most.
STAMP_CHECK_S = 2.0


class ConnectorHealth(enum.Enum):
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNMOUNTED = "unmounted"


@dataclass(frozen=True)
class ConnectorSpec:
    """What a connector IS: its name, the kind that builds it, and its settings."""

    name: str
    kind: str
    settings: tuple[tuple[str, str], ...] = ()

    @classmethod
    def of(cls, name: str, kind: str, settings: dict[str, Any] | None = None) -> ConnectorSpec:
        items = {str(k): str(v) for k, v in (settings or {}).items() if v is not None}
        return cls(name=name, kind=kind, settings=tuple(sorted(items.items())))

    def kwargs(self) -> dict[str, str]:
        return dict(self.settings)


@dataclass(frozen=True)
class ConnectorStatus:
    name: str
    kind: str
    health: ConnectorHealth
    reason: str = ""
    capabilities: tuple[str, ...] = ()
    probed_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "health": self.health.value,
            "reason": self.reason,
            "capabilities": list(self.capabilities),
            "probed_at": self.probed_at,
        }


class ConnectorUnavailable(LookupError):
    """The name does not resolve to a ready (or degraded but serving) connector."""


@dataclass
class _Instance:
    provider: Any
    spec: ConnectorSpec
    leases: int = 0
    retired: bool = False


@dataclass
class _Mount:
    spec: ConnectorSpec
    current: _Instance | None = None
    status: ConnectorStatus | None = None
    retired: list[_Instance] = field(default_factory=list)


SpecLoader = Callable[[], dict[str, ConnectorSpec]]


def connector_file_specs(state_dir: Path | None = None) -> dict[str, ConnectorSpec]:
    """Every configured connector whose ``kind`` has a storage provider.

    Reads the connector files the platform already keeps (one TOML per
    connector). A connector's ``params`` are its settings; ``credential_ref`` is
    passed through as one more setting.
    """
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import list_connectors

    kinds = set(registry.available_storage_connectors())
    out: dict[str, ConnectorSpec] = {}
    for c in list_connectors(state_dir=state_dir):
        if c.kind not in kinds:
            continue
        settings = dict(c.params)
        if c.credential_ref:
            settings.setdefault("credential_ref", c.credential_ref)
        out[c.name] = ConnectorSpec.of(c.name, c.kind, settings)
    return out


def default_spec(state_dir: Path | None = None) -> ConnectorSpec:
    """The always-mounted ``default`` connector (ADR-220 §6): no configuration needed.

    The platform's own directory store over the install's storage: the state
    directory on a laptop, or ``AXIOM_STORAGE_DEFAULT_ROOT`` (a node install sets
    it to the data volume). A deployment that has an object store points the
    default at it by configuration: ``AXIOM_STORAGE_DEFAULT_KIND`` plus
    ``AXIOM_STORAGE_DEFAULT_SETTINGS`` (a JSON object), which a chart sets.
    A configured connector named ``default`` replaces all of this.
    """
    import json
    import os

    from axiom.infra.paths import get_user_state_dir

    kind = os.environ.get("AXIOM_STORAGE_DEFAULT_KIND", "").strip() or "local"
    raw = os.environ.get("AXIOM_STORAGE_DEFAULT_SETTINGS", "").strip()
    settings: dict[str, Any] = json.loads(raw) if raw else {}
    if not isinstance(settings, dict):
        raise ValueError("AXIOM_STORAGE_DEFAULT_SETTINGS must be a JSON object")
    if kind == "local":
        root = os.environ.get("AXIOM_STORAGE_DEFAULT_ROOT", "").strip()
        settings.setdefault("root", root or str((state_dir or get_user_state_dir()) / "storage"))
    return ConnectorSpec.of(DEFAULT_CONNECTOR, kind, settings)


@dataclass(frozen=True)
class RoleStatus:
    """Which connector serves a role, and what an operator should know about it."""

    role: str
    source: str  # "mapped" | "default" | "absent"
    connector: str | None
    kind: str | None
    health: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "source": self.source,
            "connector": self.connector,
            "kind": self.kind,
            "health": self.health,
            "note": self.note,
        }


def reload_stamp_path(state_dir: Path | None = None) -> Path:
    from axiom.infra.paths import get_user_state_dir

    return (state_dir or get_user_state_dir()) / "connector" / "storage-reload.stamp"


def touch_reload_stamp(state_dir: Path | None = None) -> Path:
    """Tell every manager on this host to re-read its configuration on next use."""
    path = reload_stamp_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{time.time()}\n", encoding="utf-8")
    return path


class StorageConnectorManager:
    """Live storage connectors by name, built from configuration (ADR-220)."""

    def __init__(
        self,
        *,
        load_specs: SpecLoader | None = None,
        build: Callable[[ConnectorSpec], Any] | None = None,
        stamp: Path | None = None,
        clock: Callable[[], float] = time.time,
        default: Callable[[], ConnectorSpec] | None = None,
        load_roles: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        loader = load_specs or connector_file_specs
        make_default = default or default_spec

        def _with_default() -> dict[str, ConnectorSpec]:
            specs = dict(loader())
            specs.setdefault(DEFAULT_CONNECTOR, make_default())  # configured one wins
            return specs

        self._load_specs = _with_default
        self._load_roles = load_roles or load_role_map
        self._role_map: dict[str, str] | None = None
        self._build = build or self._registry_build
        self._stamp = stamp
        self._clock = clock
        self._lock = threading.RLock()
        self._mounts: dict[str, _Mount] = {}
        self._loaded = False
        self._stamp_seen: float | None = None
        self._stamp_checked = 0.0

    # ---- resolution -------------------------------------------------------

    @contextmanager
    def lease(self, name: str) -> Iterator[Any]:
        """The provider for ``name``, held for the duration of the block."""
        inst = self._acquire(name)
        try:
            yield inst.provider
        finally:
            self._release(inst)

    def get(self, name: str) -> Any:
        """The current provider for ``name`` (unleased: for a single short call)."""
        inst = self._acquire(name)
        self._release(inst)
        return inst.provider

    def names(self) -> list[str]:
        self._ensure_loaded()
        with self._lock:
            return sorted(self._mounts)

    def status(self, name: str | None = None) -> dict[str, ConnectorStatus]:
        self._ensure_loaded()
        with self._lock:
            mounts = (
                self._mounts
                if name is None
                else {k: v for k, v in self._mounts.items() if k == name}
            )
            return {
                k: m.status
                or ConnectorStatus(
                    k,
                    m.spec.kind,
                    ConnectorHealth.UNMOUNTED,
                    "not built yet: it mounts on first use",
                )
                for k, m in mounts.items()
            }

    # ---- roles (ADR-220 §6) -----------------------------------------------

    @contextmanager
    def lease_role(self, role: str) -> Iterator[Any]:
        """The provider serving ``role``, or ``None`` when the role is absent here.

        An unmapped role, or one mapped to a connector that cannot serve, falls
        back as the role declares: to the ``default`` connector, or to ``None``
        (the feature hides itself).
        """
        name = self._serving(role)[0]
        if name is None:
            yield None
            return
        with self.lease(name) as provider:
            yield provider

    def role_status(self) -> dict[str, RoleStatus]:
        """Which connector serves every declared role, and why."""
        return {r.name: self._role_status(r.name) for r in declared_roles()}

    def _roles(self) -> dict[str, str]:
        with self._lock:
            if self._role_map is None:
                self._role_map = dict(self._load_roles())
            return self._role_map

    def _serving(self, role: str) -> tuple[str | None, str, str]:
        """``(connector or None, source, note)`` for ``role``."""
        decl = declared_role(role)
        mapped = self._roles().get(role)
        why = ""
        if mapped:
            try:
                self._acquire_release(mapped)
                return mapped, "mapped", ""
            except ConnectorUnavailable as exc:
                why = f"mapped to {mapped!r}, which cannot serve ({exc}); "
        if decl.fallback == ABSENT:
            return None, "absent", why + "not configured here, so this feature is hidden"
        return DEFAULT_CONNECTOR, "default", why + (decl.note_on_default or "")

    def _role_status(self, role: str) -> RoleStatus:
        name, source, note = self._serving(role)
        if name is None:
            return RoleStatus(role, source, None, None, "absent", note)
        try:
            self._acquire_release(name)  # report real health, not "not built yet"
        except ConnectorUnavailable:
            pass  # the status below carries why
        st = self.status(name).get(name)
        health = st.health.value if st else ConnectorHealth.UNMOUNTED.value
        if st and st.health in (ConnectorHealth.DEGRADED, ConnectorHealth.FAILED) and st.reason:
            note = f"{note}; {st.reason}" if note else st.reason
        return RoleStatus(role, source, name, st.kind if st else None, health, note)

    def _acquire_release(self, name: str) -> None:
        self._release(self._acquire(name))

    # ---- reload -----------------------------------------------------------

    def reload(self, name: str | None = None) -> dict[str, ConnectorStatus]:
        """Re-read configuration and apply it: every connector, or just ``name``.

        Every named connector is built and probed now (not lazily), so the
        result is the health an operator can act on.
        """
        registry.rediscover()
        specs = self._load_specs()
        with self._lock:
            self._loaded = True
            self._role_map = None  # re-read with the connectors
            names = set(specs) | set(self._mounts) if name is None else {name}
        out: dict[str, ConnectorStatus] = {}
        for n in sorted(names):
            out[n] = self._apply(n, specs.get(n))
        return out

    def check_stamp(self) -> bool:
        """Reload if another process asked for one since we last looked. True if reloaded."""
        if self._stamp is None:
            return False
        now = self._clock()
        if now - self._stamp_checked < STAMP_CHECK_S:
            return False
        self._stamp_checked = now
        try:
            mtime = self._stamp.stat().st_mtime
        except OSError:
            return False
        if self._stamp_seen is None:
            self._stamp_seen = mtime
            return False
        if mtime == self._stamp_seen:
            return False
        self._stamp_seen = mtime
        self.reload()
        return True

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _registry_build(spec: ConnectorSpec) -> Any:
        return registry.get_storage_connector(spec.kind, name=spec.name, **spec.kwargs())

    def _ensure_loaded(self) -> None:
        with self._lock:
            if self._loaded:
                return
        specs = self._load_specs()
        with self._lock:
            if not self._loaded:
                for n, spec in specs.items():
                    self._mounts.setdefault(n, _Mount(spec=spec))
                self._loaded = True

    def _acquire(self, name: str) -> _Instance:
        self.check_stamp()
        self._ensure_loaded()
        with self._lock:
            mount = self._mounts.get(name)
        if mount is None:
            raise ConnectorUnavailable(f"no storage connector named {name!r} is configured")
        if mount.current is None:
            self._apply(name, mount.spec)
        with self._lock:
            mount = self._mounts.get(name)
            inst = mount.current if mount else None
            if inst is None:
                why = mount.status.reason if mount and mount.status else "not mounted"
                raise ConnectorUnavailable(f"storage connector {name!r} is unavailable: {why}")
            inst.leases += 1
            return inst

    def _release(self, inst: _Instance) -> None:
        with self._lock:
            inst.leases -= 1
            if inst.retired and inst.leases <= 0:
                self._close(inst)

    def _apply(self, name: str, spec: ConnectorSpec | None) -> ConnectorStatus:
        """Bring ``name`` to ``spec``: build beside, probe, swap only on a pass."""
        with self._lock:
            mount = self._mounts.get(name)
        if spec is None:
            return self._unmount(name, mount)
        if mount is not None and mount.current is not None and mount.current.spec == spec:
            # Unchanged and serving: nothing to build. A connector that was degraded by
            # a rejected change is back to its serving configuration, so it is ready.
            with self._lock:
                if mount.status is None or mount.status.health is not ConnectorHealth.READY:
                    mount.spec = spec
                    mount.status = ConnectorStatus(
                        name,
                        spec.kind,
                        ConnectorHealth.READY,
                        "serving; configuration matches the mounted instance",
                        _caps(mount.current.provider),
                        self._clock(),
                    )
                return mount.status
        try:
            provider = self._build(spec)
            ok, detail = _probe(provider)
        except Exception as exc:  # noqa: BLE001 - isolated to this connector
            provider, ok, detail = None, False, f"{type(exc).__name__}: {exc}"
        now = self._clock()
        with self._lock:
            mount = self._mounts.setdefault(name, _Mount(spec=spec))
            if ok:
                old = mount.current
                mount.current = _Instance(provider=provider, spec=spec)
                mount.spec = spec
                mount.status = ConnectorStatus(
                    name, spec.kind, ConnectorHealth.READY, detail, _caps(provider), now
                )
                if old is not None:
                    self._retire(old)
                return mount.status
            if provider is not None:
                _close_quietly(provider)
            serving = mount.current is not None
            mount.status = ConnectorStatus(
                name,
                spec.kind,
                ConnectorHealth.DEGRADED if serving else ConnectorHealth.FAILED,
                (
                    f"new configuration not applied, previous one still serving: {detail}"
                    if serving
                    else detail
                ),
                _caps(mount.current.provider) if serving else (),
                now,
            )
            if not serving:
                mount.spec = spec
            return mount.status

    def _unmount(self, name: str, mount: _Mount | None) -> ConnectorStatus:
        with self._lock:
            kind = mount.spec.kind if mount else ""
            if mount is not None:
                self._mounts.pop(name, None)
                if mount.current is not None:
                    self._retire(mount.current)
            return ConnectorStatus(
                name, kind, ConnectorHealth.UNMOUNTED, "no longer configured", (), self._clock()
            )

    def _retire(self, inst: _Instance) -> None:
        inst.retired = True
        if inst.leases <= 0:
            self._close(inst)

    @staticmethod
    def _close(inst: _Instance) -> None:
        _close_quietly(inst.provider)


def _probe(provider: Any) -> tuple[bool, str]:
    probe = getattr(provider, "probe", None)
    if probe is None:
        return True, "built (this provider declares no probe)"
    ok, detail = probe()
    return bool(ok), str(detail)


def _caps(provider: Any) -> tuple[str, ...]:
    return tuple(sorted(c.value for c in getattr(provider, "capabilities", ())))


def _close_quietly(provider: Any) -> None:
    close = getattr(provider, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception as exc:  # noqa: BLE001 - closing an old instance never fails a reload
        log.warning("closing a retired storage connector failed: %s", exc)


_default: StorageConnectorManager | None = None
_default_lock = threading.Lock()


def default_storage_manager() -> StorageConnectorManager:
    """The process's manager over the configured connector files."""
    global _default
    with _default_lock:
        if _default is None:
            _default = StorageConnectorManager(stamp=reload_stamp_path())
        return _default


def install_reload_signal(manager: StorageConnectorManager | None = None) -> bool:
    """Reload on ``SIGHUP`` (a long-running service calls this once). False where unsupported."""
    import signal

    if not hasattr(signal, "SIGHUP"):
        return False
    target = manager or default_storage_manager()

    def _on_hup(_signum, _frame) -> None:
        threading.Thread(target=target.reload, name="storage-reload", daemon=True).start()

    signal.signal(signal.SIGHUP, _on_hup)
    return True


__all__ = [
    "RoleStatus",
    "default_spec",
    "ConnectorHealth",
    "ConnectorSpec",
    "ConnectorStatus",
    "ConnectorUnavailable",
    "StorageConnectorManager",
    "connector_file_specs",
    "default_storage_manager",
    "install_reload_signal",
    "reload_stamp_path",
    "touch_reload_stamp",
]
