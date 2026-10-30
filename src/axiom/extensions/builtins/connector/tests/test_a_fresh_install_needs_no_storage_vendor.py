# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An install with no vendor account works on its own storage (ADR-220 §6).

A fresh install has no connector configured. The ``default`` connector is
mounted anyway, every role that falls back to it is served (files written and
read back), the off-site backup role says honestly that the copy is on this
machine only, and the inbound-deposits role is absent rather than an error.
Adding a connector later and mapping a role to it moves the role; nothing that
worked stops working.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.connector.skills import roles as roles_skill
from axiom.extensions.builtins.connector.storage.manager import (
    ConnectorHealth,
    ConnectorSpec,
    StorageConnectorManager,
    connector_file_specs,
    default_spec,
)
from axiom.extensions.builtins.connector.storage.protocol import (
    FileContent,
    FileRef,
    ListParams,
)
from axiom.extensions.builtins.connector.storage.roles import load_role_map, roles_path


def _fresh(tmp_path, **kw) -> StorageConnectorManager:
    return StorageConnectorManager(
        load_specs=lambda: connector_file_specs(tmp_path),
        default=lambda: default_spec(tmp_path),
        load_roles=lambda: load_role_map(tmp_path),
        **kw,
    )


@pytest.fixture(autouse=True)
def _no_deployment_default(monkeypatch):
    for var in (
        "AXIOM_STORAGE_DEFAULT_KIND",
        "AXIOM_STORAGE_DEFAULT_SETTINGS",
        "AXIOM_STORAGE_DEFAULT_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)


def test_the_default_store_is_mounted_with_no_configuration(tmp_path):
    m = _fresh(tmp_path)
    status = m.reload()
    assert set(status) == {"default"}
    assert status["default"].health is ConnectorHealth.READY
    assert status["default"].kind == "local"


@pytest.mark.parametrize("role", ["library", "exports", "archive", "backup", "backup.offsite"])
def test_every_storage_role_works_on_the_default_alone(tmp_path, role):
    m = _fresh(tmp_path)
    with m.lease_role(role) as store:
        store.put_file(
            FileRef(vendor="local", path=f"{role}/item.bin"), FileContent(data=b"payload")
        )
        listed = [r.path for r in store.list_files(ListParams(folder=role))]
        assert listed == [f"{role}/item.bin"]
        assert store.get_file(FileRef(vendor="local", path=f"{role}/item.bin")).data == b"payload"
    assert (tmp_path / "storage" / role / "item.bin").is_file()  # under the install's state


def test_off_site_backup_says_honestly_it_is_on_this_machine_only(tmp_path):
    st = _fresh(tmp_path).role_status()["backup.offsite"]
    assert st.source == "default" and st.connector == "default"
    assert st.note == "on this machine only: add an off-site provider"


def test_inbound_deposits_are_absent_not_an_error(tmp_path):
    m = _fresh(tmp_path)
    with m.lease_role("deposits.inbound") as store:
        assert store is None
    st = m.role_status()["deposits.inbound"]
    assert st.source == "absent" and st.health == "absent" and "hidden" in st.note


def test_an_undeclared_role_is_a_programming_error_named_as_such(tmp_path):
    with pytest.raises(KeyError, match="not declared"), _fresh(tmp_path).lease_role("nope"):
        pass


def test_a_node_points_the_default_at_its_data_volume(tmp_path, monkeypatch):
    volume = tmp_path / "data-volume"
    monkeypatch.setenv("AXIOM_STORAGE_DEFAULT_ROOT", str(volume))
    m = _fresh(tmp_path)
    with m.lease_role("library") as store:
        store.put_file(FileRef(vendor="local", path="a.txt"), FileContent(data=b"x"))
    assert (volume / "a.txt").is_file()


def test_a_deployment_points_the_default_at_another_kind_by_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_STORAGE_DEFAULT_KIND", "object-store")
    monkeypatch.setenv("AXIOM_STORAGE_DEFAULT_SETTINGS", '{"bucket": "node-data"}')
    spec = default_spec(tmp_path)
    assert spec.kind == "object-store" and spec.kwargs() == {"bucket": "node-data"}


def test_a_configured_connector_named_default_replaces_the_built_in_one(tmp_path):
    other = tmp_path / "elsewhere"
    m = StorageConnectorManager(
        load_specs=lambda: {"default": ConnectorSpec.of("default", "local", {"root": str(other)})},
        load_roles=dict,
    )
    with m.lease_role("library") as store:
        store.put_file(FileRef(vendor="local", path="a.txt"), FileContent(data=b"x"))
    assert (other / "a.txt").is_file()


def test_adding_a_provider_later_moves_only_the_role_mapped_to_it(tmp_path):
    offsite = tmp_path / "offsite"
    specs = {}
    m = StorageConnectorManager(
        load_specs=lambda: dict(specs),
        default=lambda: default_spec(tmp_path),
        load_roles=lambda: load_role_map(tmp_path),
    )
    assert m.role_status()["backup.offsite"].source == "default"
    specs["team-share"] = ConnectorSpec.of("team-share", "local", {"root": str(offsite)})
    path = roles_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[roles]\n"backup.offsite" = "team-share"\n', encoding="utf-8")
    m.reload()
    roles = m.role_status()
    assert roles["backup.offsite"].source == "mapped" and roles["backup.offsite"].note == ""
    assert roles["library"].connector == "default"  # everything else keeps working as it was
    with m.lease_role("backup.offsite") as store:
        store.put_file(FileRef(vendor="local", path="b.dump"), FileContent(data=b"x"))
    assert (offsite / "b.dump").is_file()


def test_a_role_mapped_to_a_connector_that_cannot_serve_falls_back_and_says_why(tmp_path):
    blocked = tmp_path / "a-file-not-a-dir"
    blocked.write_text("x")
    path = roles_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[roles]\n"backup.offsite" = "broken"\n', encoding="utf-8")
    m = StorageConnectorManager(
        load_specs=lambda: {"broken": ConnectorSpec.of("broken", "local", {"root": str(blocked)})},
        default=lambda: default_spec(tmp_path),
        load_roles=lambda: load_role_map(tmp_path),
    )
    st = m.role_status()["backup.offsite"]
    assert st.source == "default" and "mapped to 'broken'" in st.note
    assert "on this machine only" in st.note


def test_the_roles_verb_shows_which_provider_serves_each_role(tmp_path):
    result = roles_skill.run({"manager": _fresh(tmp_path)}, SimpleNamespace(state_dir=tmp_path))
    rows = {r["role"]: r for r in result.value["roles"]}
    assert result.ok and rows["library"]["connector"] == "default"
    assert rows["library"]["kind"] == "local"
    assert rows["deposits.inbound"]["connector"] is None
