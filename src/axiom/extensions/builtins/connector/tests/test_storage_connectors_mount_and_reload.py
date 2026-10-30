# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Configured storage connectors mount by name and reload without a restart (ADR-220 §3).

Every rule of the reload contract is pinned here against a fake factory, so the
semantics are the manager's and hold for every vendor: add only on a passing
probe, change built beside and swapped only on a pass, remove closes on the last
lease, in-flight work finishes on its instance, and one connector's failure is
its own.
"""

from __future__ import annotations

import threading

import pytest

from axiom.extensions.builtins.connector.skills import reload as reload_skill
from axiom.extensions.builtins.connector.storage.manager import (
    ConnectorHealth,
    ConnectorSpec,
    ConnectorUnavailable,
    StorageConnectorManager,
    touch_reload_stamp,
)
from axiom.extensions.builtins.connector.storage.protocol import (
    MissingCapability,
    StorageCapability,
    require,
)


class Store:
    capabilities = frozenset({StorageCapability.LIST, StorageCapability.READ})
    vendor = "fake"

    def __init__(self, spec: ConnectorSpec, *, healthy: bool = True) -> None:
        self.spec, self.healthy, self.closed = spec, healthy, False

    def probe(self):
        return self.healthy, "ok" if self.healthy else "cannot reach the store"

    def close(self) -> None:
        self.closed = True


class World:
    """The configuration a test edits, and the factory the manager builds with."""

    def __init__(self) -> None:
        self.specs: dict[str, ConnectorSpec] = {}
        self.unhealthy: set[str] = set()
        self.explode: set[str] = set()
        self.built: list[Store] = []

    def set(self, name: str, **settings) -> None:
        self.specs[name] = ConnectorSpec.of(name, "fake", settings)

    def build(self, spec: ConnectorSpec) -> Store:
        if spec.name in self.explode:
            raise RuntimeError("factory could not build it")
        store = Store(spec, healthy=spec.name not in self.unhealthy)
        self.built.append(store)
        return store

    def manager(self, **kw) -> StorageConnectorManager:
        kw.setdefault("default", lambda: ConnectorSpec.of("default", "fake", {"root": "/default"}))
        kw.setdefault("load_roles", dict)
        return StorageConnectorManager(load_specs=lambda: dict(self.specs), build=self.build, **kw)


@pytest.fixture
def world() -> World:
    return World()


def test_a_configured_connector_mounts_by_name_on_first_use(world):
    world.set("share", root="/a")
    m = world.manager()
    assert m.status()["share"].health is ConnectorHealth.UNMOUNTED  # lazy: nothing built yet
    assert world.built == []
    with m.lease("share") as store:
        assert store.spec.kwargs() == {"root": "/a"}
    assert m.status()["share"].health is ConnectorHealth.READY


def test_an_unconfigured_name_does_not_resolve(world):
    with pytest.raises(ConnectorUnavailable, match="no storage connector named 'nope'"):
        world.manager().get("nope")


def test_an_added_connector_resolves_only_once_its_probe_passes(world):
    m = world.manager()
    world.set("new", root="/n")
    world.unhealthy.add("new")
    status = m.reload()["new"]
    assert status.health is ConnectorHealth.FAILED and "cannot reach" in status.reason
    with pytest.raises(ConnectorUnavailable, match="cannot reach"):
        m.get("new")
    world.unhealthy.discard("new")
    assert m.reload("new")["new"].health is ConnectorHealth.READY
    assert m.get("new").healthy


def test_a_changed_connector_is_swapped_only_on_a_passing_probe(world):
    world.set("share", root="/a")
    m = world.manager()
    first = m.get("share")
    world.set("share", root="/b")
    world.unhealthy.add("share")
    status = m.reload("share")["share"]
    assert status.health is ConnectorHealth.DEGRADED
    assert "previous one still serving" in status.reason
    assert m.get("share") is first and not first.closed  # the old one keeps serving
    world.unhealthy.discard("share")
    assert m.reload("share")["share"].health is ConnectorHealth.READY
    assert m.get("share").spec.kwargs() == {"root": "/b"}
    assert first.closed  # retired with no lease held: closed at once


def test_going_back_to_the_serving_configuration_clears_degraded(world):
    world.set("share", root="/a")
    m = world.manager()
    m.get("share")
    world.set("share", root="/b")
    world.unhealthy.add("share")
    m.reload("share")
    world.set("share", root="/a")
    assert m.reload("share")["share"].health is ConnectorHealth.READY


def test_in_flight_work_finishes_on_the_instance_it_started_on(world):
    world.set("share", root="/a")
    m = world.manager()
    with m.lease("share") as old:
        world.set("share", root="/b")
        m.reload("share")
        assert m.get("share") is not old  # new work goes to the new instance
        assert not old.closed  # the transfer in flight is not cut off
    assert old.closed  # closed when its last lease was released


def test_a_removed_connector_stops_resolving_and_closes_after_its_last_lease(world):
    world.set("share", root="/a")
    m = world.manager()
    with m.lease("share") as store:
        del world.specs["share"]
        assert m.reload()["share"].health is ConnectorHealth.UNMOUNTED
        with pytest.raises(ConnectorUnavailable):
            m.get("share")
        assert not store.closed
    assert store.closed


def test_one_connectors_failure_is_its_own(world):
    world.set("good", root="/g")
    world.set("bad", root="/b")
    world.explode.add("bad")
    statuses = world.manager().reload()
    assert statuses["good"].health is ConnectorHealth.READY
    assert statuses["bad"].health is ConnectorHealth.FAILED
    assert "factory could not build it" in statuses["bad"].reason


def test_an_unchanged_connector_is_not_rebuilt_on_reload(world):
    world.set("share", root="/a")
    m = world.manager()
    first = m.get("share")
    m.reload()
    assert m.get("share") is first
    assert [b.spec.name for b in world.built].count("share") == 1


def test_health_carries_the_declared_capabilities(world):
    world.set("share", root="/a")
    status = world.manager().reload()["share"]
    assert status.capabilities == ("list", "read")
    assert status.as_dict()["health"] == "ready"


def test_a_reload_asked_for_in_another_process_is_picked_up_on_next_use(world, tmp_path):
    import os

    now = [1000.0]
    world.set("share", root="/a")
    stamp = touch_reload_stamp(tmp_path)  # <tmp>/connector/storage-reload.stamp
    m = world.manager(stamp=stamp, clock=lambda: now[0])
    first = m.get("share")  # first look records the stamp; no reload
    world.set("share", root="/b")
    os.utime(stamp, (now[0] + 5, now[0] + 5))
    now[0] += 1  # inside the check interval: not looked at yet
    assert m.get("share") is first
    now[0] += 5
    assert m.get("share").spec.kwargs() == {"root": "/b"}


def test_concurrent_leases_and_a_reload_never_hand_out_a_closed_store(world):
    world.set("share", root="/0")
    m = world.manager()
    seen_closed = []

    def worker():
        for _ in range(200):
            with m.lease("share") as store:
                if store.closed:
                    seen_closed.append(store)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for i in range(1, 30):
        world.set("share", root=f"/{i}")
        m.reload("share")
    for t in threads:
        t.join()
    assert seen_closed == []


def test_a_missing_capability_is_refused_before_the_call_naming_the_gap(world):
    world.set("share", root="/a")
    store = world.manager().get("share")
    require(store, {StorageCapability.READ})
    with pytest.raises(MissingCapability, match="'share'.*'fake'.*write"):
        require(store, {StorageCapability.READ, StorageCapability.WRITE}, connector="share")


def test_the_reload_verb_reports_health_and_fails_on_a_failed_connector(world, tmp_path):
    from types import SimpleNamespace

    world.set("good", root="/g")
    world.set("bad", root="/b")
    world.unhealthy.add("bad")
    ctx = SimpleNamespace(state_dir=tmp_path)
    result = reload_skill.run({"manager": world.manager()}, ctx)
    assert not result.ok and result.errors == ["bad: cannot reach the store"]
    rows = {r["name"]: r["health"] for r in result.value["connectors"]}
    assert rows == {"good": "ready", "bad": "failed", "default": "ready"}
    assert (tmp_path / "connector" / "storage-reload.stamp").is_file()
    world.unhealthy.discard("bad")
    assert reload_skill.run({"manager": world.manager(), "no_stamp": True}, ctx).ok
